"""Claude-drafted recommendations for review items (PRD feature C).

The rules engine has already decided what is risky; Claude reads those findings and drafts a
keep / revoke / modify suggestion with a short rationale that cites them. A person still
makes every decision, and the decision records whether it followed the suggestion.

What is sent to Claude, per flagged item: the application, entitlement, privilege tier,
account type and status, days since last login, the person's department, title, employment
status and dates, and each finding's rule, severity, description and non-identifying inputs.
No names, emails, usernames or account IDs are sent; items are labelled I1, I2, ... and
findings I1-F1, ... The exact payload's SHA-256 and the full suggestions are written to the
evidence ledger, so an auditor can see what was asked and what came back.

Every suggestion is validated before it is stored: it must answer an item that was sent,
use an allowed action, and cite at least one of that item's own findings. Anything else is
dropped and reported, never guessed at.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import llm
from backend.db.models import (
    Account,
    AccountEntitlement,
    Application,
    Campaign,
    CampaignState,
    Entitlement,
    Finding,
    IdentityLink,
    Person,
    ReviewAction,
    ReviewItem,
)
from backend.domain.evidence import record_event
from backend.rule_registry import RULE_SET_VERSION, RULES

log = logging.getLogger("uar.recommendations")

PROMPT_VERSION = "2026-11-16.1"
ACTIONS = ("keep", "revoke", "modify")
CHUNK = 20  # items per request: keeps each answer well inside max_tokens
ALLOWED_STATES = {CampaignState.analyzing, CampaignState.in_review}
# Inputs that identify a person or account; never sent.
IDENTIFYING = {"user_id", "email", "username", "name", "display_name", "native_id", "employee_id", "manager"}
RULE_TEXT = {r["id"]: r["description"] for r in RULES}

SYSTEM = """You help a company's security team run a user access review for SOC 2.

A deterministic rules engine has already examined every account and attached findings to the
risky ones. For each review item below, draft a suggestion for the human reviewer:
- "revoke": remove the access.
- "modify": keep the account but reduce the access (say what to reduce it to).
- "keep": the access looks justified despite the findings.

Ground every suggestion in the findings you are given and cite them by their ids (e.g. "I3-F1").
Do not invent facts that are not in the item. Where the deciding fact is missing (for example,
whether a contractor's engagement is still active), say what the reviewer should confirm and
mark your confidence "low".

Typical judgements: a terminated person's active access should be revoked; privileged access
for an active employee whose title fits it is usually kept but worth confirming; an account
unused for months is a candidate for revoke; external accounts need an owner to vouch for them.

Write each rationale in plain English for a busy manager: at most three sentences, under 400
characters, no markdown."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "The item id, e.g. I1"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "rationale": {"type": "string"},
                    "cited_findings": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["item", "action", "rationale", "cited_findings", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


class Suggestion(BaseModel):
    item: str
    action: str
    rationale: str
    cited_findings: List[str]
    confidence: str


class Answer(BaseModel):
    recommendations: List[Suggestion]


class RecommendationError(ValueError):
    pass


def _days_since(value: Optional[datetime], today: date) -> Optional[int]:
    if value is None:
        return None
    return (today - value.date()).days


def _clean_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in sorted((inputs or {}).items()) if k not in IDENTIFYING and v not in (None, "")}


def build_payload(session: Session, campaign: Campaign) -> Tuple[List[Dict[str, Any]], Dict[str, ReviewItem],
                                                                   Dict[str, uuid.UUID]]:
    """The de-identified items to send, plus maps from the I/F labels back to real ids."""
    items = [i for i in session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id))
             if i.severity != "none" and i.finding_ids]
    if not items:
        return [], {}, {}
    grants = {g.id: g for g in session.scalars(select(AccountEntitlement).where(
        AccountEntitlement.id.in_([i.account_entitlement_id for i in items])))}
    accounts = {a.id: a for a in session.scalars(select(Account).where(
        Account.id.in_([g.account_id for g in grants.values()])))}
    ents = {e.id: e for e in session.scalars(select(Entitlement))}
    apps = {a.id: a.name for a in session.scalars(select(Application))}
    links = {l.account_id: l for l in session.scalars(select(IdentityLink).where(
        IdentityLink.account_id.in_(list(accounts))))}
    people = {p.id: p for p in session.scalars(select(Person))}
    findings = {str(f.id): f for f in session.scalars(select(Finding).where(Finding.campaign_id == campaign.id))}

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items.sort(key=lambda i: (order.get(i.severity, 9), str(i.id)))
    today = datetime.now(timezone.utc).date()
    payload, item_by_label, finding_by_label = [], {}, {}
    for n, item in enumerate(items, start=1):
        label = f"I{n}"
        g = grants[item.account_entitlement_id]
        a = accounts[g.account_id]
        e = ents[g.entitlement_id]
        link = links.get(a.id)
        p = people.get(link.person_id) if link and link.person_id else None
        fs = []
        item_findings = sorted((findings[str(fid)] for fid in item.finding_ids or [] if str(fid) in findings),
                               key=lambda f: (order.get(f.severity, 9), f.rule_id))  # most severe first
        for m, f in enumerate(item_findings, start=1):
            flabel = f"{label}-F{m}"
            finding_by_label[flabel] = f.id
            fs.append({"id": flabel, "rule": f.rule_id, "severity": f.severity,
                       "description": RULE_TEXT.get(f.rule_id, f.rule_id), "inputs": _clean_inputs(f.inputs)})
        item_by_label[label] = item
        payload.append({
            "item": label,
            "application": apps.get(a.application_id, ""),
            "entitlement": e.name,
            "privilege_tier": e.privilege_tier,
            "account_type": a.account_type.value,
            "account_status": a.status,
            "days_since_last_login": _days_since(a.last_login_at, today),
            "person": None if p is None else {
                "department": p.department, "title": p.title, "employment_status": p.employment_status,
                "hire_date": p.hire_date.isoformat() if p.hire_date else None,
                "termination_date": p.termination_date.isoformat() if p.termination_date else None,
            },
            "findings": fs,
        })
    return payload, item_by_label, finding_by_label


def _ask(client: Any, chunk: List[Dict[str, Any]]) -> Tuple[Answer, str]:
    """One request to Claude for up to CHUNK items. Returns the parsed answer and the model that served it."""
    response = client.beta.messages.create(
        model=llm.MODEL,
        max_tokens=16000,
        betas=[llm.FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": "Review items (JSON):\n" + json.dumps(chunk, indent=1, default=str)}],
    )
    if response.stop_reason == "refusal":
        raise RecommendationError("Claude declined to draft suggestions for some items.")
    if response.stop_reason == "max_tokens":
        raise RecommendationError("Claude's answer was cut off; try again.")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    try:
        return Answer.model_validate_json(text), getattr(response, "model", llm.MODEL)
    except ValidationError as exc:
        raise RecommendationError("Claude's answer didn't match the expected format.") from exc


def generate(session: Session, campaign: Campaign, actor: str) -> Dict[str, Any]:
    if campaign.state not in ALLOWED_STATES:
        raise RecommendationError("Suggestions can be drafted after analysis and until sign-off "
                                  f"(the campaign is {campaign.state.value}).")
    payload, item_by_label, finding_by_label = build_payload(session, campaign)
    if not payload:
        return {"suggested": 0, "skipped": [], "items_sent": 0, "model": None}
    client = llm.client_factory()

    accepted: Dict[str, Suggestion] = {}
    skipped: List[str] = []
    served: set = set()
    for start in range(0, len(payload), CHUNK):
        chunk = payload[start:start + CHUNK]
        answer, model = _ask(client, chunk)
        served.add(model)
        sent = {c["item"]: {f["id"] for f in c["findings"]} for c in chunk}
        for s in answer.recommendations:
            if s.item not in sent or s.item in accepted:
                continue
            cites = [c for c in s.cited_findings if c in sent[s.item]]
            if s.action not in ACTIONS or not cites or not s.rationale.strip():
                skipped.append(s.item)
                continue
            accepted[s.item] = Suggestion(item=s.item, action=s.action, rationale=s.rationale.strip()[:600],
                                          cited_findings=cites, confidence=s.confidence)
        skipped.extend(label for label in sent if label not in accepted and label not in skipped)

    now = datetime.now(timezone.utc)
    run_id = uuid.uuid4()
    stored = []
    for label, s in accepted.items():
        item = item_by_label[label]
        item.recommendation = ReviewAction(s.action)
        item.rationale = s.rationale
        item.recommendation_meta = {
            "source": "claude", "model": ", ".join(sorted(served)), "prompt_version": PROMPT_VERSION,
            "confidence": s.confidence, "cited_finding_ids": [str(finding_by_label[c]) for c in s.cited_findings],
            "generated_at": now.isoformat(), "generated_by": actor, "run_id": str(run_id),
        }
        stored.append({"review_item_id": str(item.id), "action": s.action, "confidence": s.confidence,
                       "rationale": s.rationale, "cited_finding_ids": item.recommendation_meta["cited_finding_ids"]})
    session.flush()

    sent_bytes = json.dumps(payload, sort_keys=True, default=str).encode()
    record_event(session, campaign, "recommendation.generated", {
        "run_id": run_id, "model_requested": llm.MODEL, "models_served": sorted(served),
        "prompt_version": PROMPT_VERSION, "rule_set_version": RULE_SET_VERSION,
        "items_sent": len(payload), "payload_sha256": hashlib.sha256(sent_bytes).hexdigest(),
        "suggestions": stored, "skipped_items": [str(item_by_label[l].id) for l in skipped],
    }, actor)
    log.info("recommendations generated", extra={"campaign": str(campaign.id), "items": len(payload),
                                                  "accepted": len(stored)})
    return {"suggested": len(stored), "skipped": len(skipped), "items_sent": len(payload),
            "model": ", ".join(sorted(served))}

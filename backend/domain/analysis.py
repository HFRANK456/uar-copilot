"""Campaign analysis (FR-09, FR-11): run the versioned rule set over every
account in the campaign, store findings, and create review items.

A review item is one person's one entitlement in one application during one
campaign; that is the unit auditors sample. Accounts with no listed roles get
a synthetic "Account access" entitlement so they are still reviewed.

Findings come only from the deterministic rules engine. Each stores the rule
ID and version, the exact input values, and the account it applies to, so it
can be re-performed without this code's runtime state.
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

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
    ReviewItem,
)
from backend.domain.campaigns import InvalidTransition, transition
from backend.domain.evidence import record_event
from backend.domain.identity import campaign_accounts, identity_summary
from backend.rule_registry import RULE_SET_VERSION, RULES
from backend.rules import SEVERITY_PRIORITY, compute_flags, evaluate_rules

ACCOUNT_ACCESS = "Account access"
ENTITLEMENT_FIELDS = {"privilege_tier", "privilege_category", "is_privileged", "is_admin", "role"}


class NotReady(ValueError):
    pass


def rule_scope(rule: Dict[str, Any]) -> str:
    """'entitlement' if the rule looks at what access is held, else 'account'."""
    if "scope" in rule:
        return rule["scope"]
    return "entitlement" if any(c["field"] in ENTITLEMENT_FIELDS for c in rule["conditions"]) else "account"


@dataclass
class Row:
    account: Account
    grant: AccountEntitlement
    entitlement: Entitlement
    person: Optional[Person]


def _ensure_account_access(session: Session, campaign: Campaign, account: Account, actor: str) -> AccountEntitlement:
    ent = session.scalar(
        select(Entitlement).where(Entitlement.application_id == account.application_id, Entitlement.name == ACCOUNT_ACCESS)
    )
    if ent is None:
        ent = Entitlement(
            tenant_id=campaign.tenant_id,
            application_id=account.application_id,
            name=ACCOUNT_ACCESS,
            description="Access to the application with no specific role listed in the source",
            privilege_tier="none",
            category="standard_access",
            created_by=actor,
        )
        session.add(ent)
        session.flush()
    grant = AccountEntitlement(tenant_id=campaign.tenant_id, account_id=account.id, entitlement_id=ent.id, created_by=actor)
    session.add(grant)
    session.flush()
    return grant


def _rows(session: Session, campaign: Campaign, actor: str) -> List[Row]:
    accounts = campaign_accounts(session, campaign)
    account_ids = [a.id for a in accounts]
    grants: Dict[uuid.UUID, List[AccountEntitlement]] = defaultdict(list)
    for g in session.scalars(select(AccountEntitlement).where(AccountEntitlement.account_id.in_(account_ids))):
        grants[g.account_id].append(g)
    entitlements = {e.id: e for e in session.scalars(select(Entitlement))}
    links = {
        link.account_id: link
        for link in session.scalars(select(IdentityLink).where(IdentityLink.account_id.in_(account_ids)))
    }
    people = {p.id: p for p in session.scalars(select(Person))}

    rows: List[Row] = []
    for a in accounts:
        account_grants = grants.get(a.id) or [_ensure_account_access(session, campaign, a, actor)]
        link = links.get(a.id)
        person = people.get(link.person_id) if link and link.person_id else None
        for g in account_grants:
            ent = entitlements.get(g.entitlement_id) or session.get(Entitlement, g.entitlement_id)
            rows.append(Row(a, g, ent, person))
    return rows


def _naive_utc(ts: Optional[datetime]) -> Optional[pd.Timestamp]:
    if ts is None:
        return None
    return pd.Timestamp(ts.astimezone(timezone.utc).replace(tzinfo=None))


def _frame(rows: List[Row]) -> pd.DataFrame:
    records = []
    for r in rows:
        p = r.person
        records.append(
            {
                "user_id": r.account.native_id,
                "status": r.account.status if r.account.status is not None else "active",
                "role": r.entitlement.name if r.entitlement.name != ACCOUNT_ACCESS else "",
                "termination_date": p.termination_date if p else None,
                "employment_status": p.employment_status if p else None,
                "account_type": r.account.account_type.value,
                "last_login_at": _naive_utc(r.account.last_login_at),
                "person": (p.name or p.email) if p else None,
                "entitlement": r.entitlement.name,
            }
        )
    return pd.DataFrame.from_records(records)


def _pick_reviewer(
    campaign: Campaign, person: Optional[Person], app: Application, people: Dict[uuid.UUID, Person]
) -> Tuple[Optional[uuid.UUID], str]:
    """Return (reviewer person id, reason). Never the person whose access it is."""
    subject = person.id if person else None
    candidates: List[Tuple[Optional[uuid.UUID], str]] = []
    if campaign.reviewer_rule == "app_owner":
        candidates.append((app.owner_person_id, "app_owner"))
    if person is not None:
        candidates.append((person.manager_id, "manager"))
        manager = people.get(person.manager_id) if person.manager_id else None
        candidates.append((manager.manager_id if manager else None, "skip_level_manager"))
    candidates.append((app.owner_person_id, "app_owner"))
    for reviewer, reason in candidates:
        if reviewer and reviewer != subject:
            return reviewer, reason
    return None, "unassigned"


def analyze(session: Session, campaign: Campaign, actor: str, actor_kind: str = "human",
            as_of: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    if campaign.state not in (CampaignState.resolving_identities, CampaignState.analyzing):
        raise InvalidTransition(f"cannot analyze a campaign in state {campaign.state.value}")
    readiness = identity_summary(session, campaign)
    if not readiness["ready_for_analysis"]:
        raise NotReady(f"{readiness['open']} account(s) still need an identity decision")

    # A re-run replaces the previous analysis; no decisions exist before review starts.
    session.execute(delete(ReviewItem).where(ReviewItem.campaign_id == campaign.id))
    session.execute(delete(Finding).where(Finding.campaign_id == campaign.id))

    rows = _rows(session, campaign, actor)
    as_of = (as_of or pd.Timestamp.now(tz="UTC").tz_localize(None)).normalize()
    df = compute_flags(_frame(rows), as_of)
    results = evaluate_rules(df, as_of=as_of)
    scopes = {r["id"]: rule_scope(r) for r in RULES}

    # Store each finding once: per account for account rules, per grant for entitlement rules.
    findings_by_row: Dict[int, List[Finding]] = defaultdict(list)
    account_level: Dict[Tuple[str, uuid.UUID], Finding] = {}
    for res in results:
        row = rows[int(res["row_ref"])]
        scope = scopes.get(res["issue_type"], "account")
        if scope == "account":
            key = (res["issue_type"], row.account.id)
            finding = account_level.get(key)
            if finding is None:
                inputs = {k: v for k, v in res["inputs"].items() if k not in ENTITLEMENT_FIELDS}
                finding = Finding(
                    tenant_id=campaign.tenant_id, campaign_id=campaign.id, rule_id=res["issue_type"],
                    rule_version=res["rule_version"], severity=res["severity"], account_id=row.account.id,
                    inputs={**inputs, "application": None, "scope": "account"},
                    explanation=res["explanation"], created_by=actor,
                )
                account_level[key] = finding
                session.add(finding)
        else:
            finding = Finding(
                tenant_id=campaign.tenant_id, campaign_id=campaign.id, rule_id=res["issue_type"],
                rule_version=res["rule_version"], severity=res["severity"], account_id=row.account.id,
                inputs={**res["inputs"], "entitlement": row.entitlement.name, "scope": "entitlement"},
                explanation=res["explanation"], created_by=actor,
            )
            session.add(finding)
        findings_by_row[int(res["row_ref"])].append(finding)
    session.flush()

    apps = {a.id: a for a in session.scalars(select(Application))}
    for f in account_level.values():
        acct = next(r.account for r in rows if r.account.id == f.account_id)
        f.inputs = {**f.inputs, "application": apps[acct.application_id].name}
    people = {p.id: p for p in session.scalars(select(Person))}

    severity_counts: Counter = Counter()
    reviewer_reasons: Counter = Counter()
    for i, row in enumerate(rows):
        row_findings = findings_by_row.get(i, [])
        severity = max((f.severity for f in row_findings), key=lambda s: SEVERITY_PRIORITY[s], default="none")
        reviewer, reason = _pick_reviewer(campaign, row.person, apps[row.account.application_id], people)
        session.add(
            ReviewItem(
                tenant_id=campaign.tenant_id, campaign_id=campaign.id, account_entitlement_id=row.grant.id,
                reviewer_id=reviewer, finding_ids=sorted({str(f.id) for f in row_findings}),
                severity=severity, status="pending", created_by=actor,
            )
        )
        severity_counts[severity] += 1
        reviewer_reasons[reason] += 1
    session.flush()

    finding_counts = Counter(f.severity for f in session.scalars(select(Finding).where(Finding.campaign_id == campaign.id)))
    summary = {
        "review_items": len(rows),
        "items_by_severity": {s: severity_counts.get(s, 0) for s in ("critical", "high", "medium", "low", "none")},
        "findings": sum(finding_counts.values()),
        "findings_by_severity": {s: finding_counts.get(s, 0) for s in ("critical", "high", "medium", "low")},
        "reviewers": dict(reviewer_reasons),
        "unassigned": reviewer_reasons.get("unassigned", 0),
        "rule_set_version": RULE_SET_VERSION,
        "as_of": as_of.date().isoformat(),
    }
    campaign.rule_set_version = RULE_SET_VERSION
    if campaign.state == CampaignState.resolving_identities:
        transition(session, campaign, CampaignState.analyzing, actor, actor_kind)
    record_event(session, campaign, "analysis.completed",
                 {**summary, "rules": [{"id": r["id"], "version": r["version"]} for r in RULES]}, actor, actor_kind)
    return summary


def analysis_summary(session: Session, campaign: Campaign) -> Optional[Dict[str, Any]]:
    """Counts from the stored analysis, or None if it hasn't run."""
    items = list(session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id)))
    if not items:
        return None
    findings = list(session.scalars(select(Finding).where(Finding.campaign_id == campaign.id)))
    sev = Counter(i.severity for i in items)
    fsev = Counter(f.severity for f in findings)
    return {
        "review_items": len(items),
        "items_by_severity": {s: sev.get(s, 0) for s in ("critical", "high", "medium", "low", "none")},
        "findings": len(findings),
        "findings_by_severity": {s: fsev.get(s, 0) for s in ("critical", "high", "medium", "low")},
        "unassigned": sum(1 for i in items if i.reviewer_id is None),
        "rule_set_version": campaign.rule_set_version,
    }

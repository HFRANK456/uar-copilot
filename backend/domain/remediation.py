"""Remediation (FR-18, FR-19): turn revoke/modify decisions into tracked changes,
then confirm each change with a later access list before closing the campaign.

Lifecycle of one remediation:
    open      created at sign-off from a final revoke or modify decision
    done      someone recorded that the change was made (optionally with a ticket number)
    verified  a later upload of that application's access list shows the access is gone
    accepted  closed without a re-upload, with a written reason (e.g. no export exists)

The campaign closes (remediating -> verifying -> closed) only when every remediation
is verified or accepted. Every step is an evidence event.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import (
    Account,
    AccountEntitlement,
    Application,
    Campaign,
    CampaignState,
    Decision,
    Entitlement,
    IdentityLink,
    Person,
    Remediation,
    ReviewAction,
    ReviewItem,
    SourceKind,
)
from backend.domain.campaigns import InvalidTransition, transition
from backend.domain.evidence import record_event
from backend.domain.ingest import IngestError, _check_size, _clean, _read_table, _snapshot
from backend.mapping import ACCESS_LIST_ALIASES, map_columns
from backend.rules import _normalize_status_value
from schemas.user_schema import ColumnMapping

OPEN_STATES = {"open", "done"}
RESOLVED_STATES = {"verified", "accepted"}
MIN_REASON = 10


class RemediationError(ValueError):
    pass


def _owner(campaign: Campaign, actor: str, what: str) -> None:
    from backend.domain.review import require_owner

    require_owner(campaign, actor, what)


# ---------------------------------------------------------------------------
# Create at sign-off
# ---------------------------------------------------------------------------


def create_for_campaign(session: Session, campaign: Campaign, actor: str) -> int:
    """One remediation per item whose final decision is revoke or modify."""
    items = list(session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id)))
    latest: Dict[uuid.UUID, Decision] = {}
    for d in session.scalars(
        select(Decision).where(Decision.review_item_id.in_([i.id for i in items])).order_by(Decision.decided_at)
    ):
        latest[d.review_item_id] = d
    created = 0
    for item in items:
        d = latest.get(item.id)
        if d is None or d.action not in (ReviewAction.revoke, ReviewAction.modify):
            continue
        session.add(Remediation(
            tenant_id=campaign.tenant_id, campaign_id=campaign.id, review_item_id=item.id, decision_id=d.id,
            action=d.action.value, status="open", created_by=actor,
        ))
        created += 1
    session.flush()
    return created


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def rows(session: Session, campaign: Campaign) -> List[Dict[str, Any]]:
    rems = list(session.scalars(select(Remediation).where(Remediation.campaign_id == campaign.id)))
    if not rems:
        return []
    items = {i.id: i for i in session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id))}
    grants = {g.id: g for g in session.scalars(select(AccountEntitlement).where(
        AccountEntitlement.id.in_([i.account_entitlement_id for i in items.values()])))}
    accounts = {a.id: a for a in session.scalars(select(Account).where(
        Account.id.in_([g.account_id for g in grants.values()])))}
    ents = {e.id: e for e in session.scalars(select(Entitlement))}
    apps = {a.id: a for a in session.scalars(select(Application))}
    links = {l.account_id: l for l in session.scalars(select(IdentityLink).where(
        IdentityLink.account_id.in_(list(accounts))))}
    people = {p.id: p for p in session.scalars(select(Person))}
    decisions = {d.id: d for d in session.scalars(select(Decision).where(
        Decision.id.in_([r.decision_id for r in rems])))}

    out = []
    for r in rems:
        item = items.get(r.review_item_id)
        g = grants.get(item.account_entitlement_id) if item else None
        a = accounts.get(g.account_id) if g else None
        e = ents.get(g.entitlement_id) if g else None
        link = links.get(a.id) if a else None
        p = people.get(link.person_id) if link and link.person_id else None
        d = decisions.get(r.decision_id)
        out.append({
            "id": r.id, "status": r.status, "action": r.action, "severity": item.severity if item else None,
            "application_id": a.application_id if a else None,
            "application": apps[a.application_id].name if a else None,
            "account": a.native_id if a else None, "entitlement": e.name if e else None,
            "person": (p.name or p.email) if p else None,
            "change": d.justification if d else None,
            "decided_by": d.actor_id if d else None,
            "ticket_ref": r.ticket_ref, "note": r.note, "resolved_by": r.resolved_by, "resolved_at": r.resolved_at,
            "verified_at": r.verified_at, "verification_method": r.verification_method,
            "verified_snapshot_id": r.verified_snapshot_id,
        })
    order = {"open": 0, "done": 1, "verified": 2, "accepted": 3}
    out.sort(key=lambda x: (order.get(x["status"], 9), x["application"] or "", x["account"] or ""))
    return out


def summary(session: Session, campaign: Campaign) -> Dict[str, Any]:
    rs = rows(session, campaign)
    counts = {s: sum(1 for r in rs if r["status"] == s) for s in ("open", "done", "verified", "accepted")}
    return {"total": len(rs), **counts,
            "ready_to_close": campaign.state == CampaignState.remediating and all(
                r["status"] in RESOLVED_STATES for r in rs)}


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def _load(session: Session, campaign: Campaign, rem_id: uuid.UUID) -> Remediation:
    r = session.scalar(select(Remediation).where(Remediation.id == rem_id, Remediation.campaign_id == campaign.id))
    if r is None:
        raise LookupError("remediation not found")
    return r


def _require_remediating(campaign: Campaign) -> None:
    if campaign.state != CampaignState.remediating:
        raise InvalidTransition(f"remediation can only change while the campaign is remediating "
                                f"(now {campaign.state.value})")


def update(session: Session, campaign: Campaign, rem_id: uuid.UUID, actor: str, *, status: Optional[str] = None,
           ticket_ref: Optional[str] = None, note: Optional[str] = None) -> Remediation:
    """Record a ticket number and/or that the change was made."""
    _owner(campaign, actor, "update remediation")
    _require_remediating(campaign)
    r = _load(session, campaign, rem_id)
    if r.status in RESOLVED_STATES:
        raise RemediationError(f"this remediation is already {r.status}")
    if status not in (None, "done"):
        raise RemediationError("status can only be set to done here; verify with an upload or accept with a reason")
    changes: Dict[str, Any] = {}
    if ticket_ref is not None:
        r.ticket_ref = ticket_ref.strip() or None
        changes["ticket_ref"] = r.ticket_ref
    if note is not None:
        r.note = note.strip() or None
        changes["note"] = r.note
    if status == "done":
        r.status = "done"
        r.resolved_by = actor
        r.resolved_at = datetime.now(timezone.utc)
        changes["status"] = "done"
    if not changes:
        raise RemediationError("nothing to update")
    session.flush()
    record_event(session, campaign, "remediation.updated", {"remediation_id": r.id, **changes}, actor)
    return r


def accept(session: Session, campaign: Campaign, rem_id: uuid.UUID, actor: str, reason: str) -> Remediation:
    """Close one remediation without a re-upload, e.g. when the system has no export."""
    _owner(campaign, actor, "accept remediation")
    _require_remediating(campaign)
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise RemediationError(f"say why this can't be verified by upload (at least {MIN_REASON} characters)")
    r = _load(session, campaign, rem_id)
    if r.status in RESOLVED_STATES:
        raise RemediationError(f"this remediation is already {r.status}")
    r.status = "accepted"
    r.note = reason
    r.verification_method = "accepted"
    r.verified_at = datetime.now(timezone.utc)
    r.resolved_by = r.resolved_by or actor
    r.resolved_at = r.resolved_at or r.verified_at
    session.flush()
    record_event(session, campaign, "remediation.accepted", {"remediation_id": r.id, "reason": reason}, actor)
    return r


# ---------------------------------------------------------------------------
# Verify by re-upload
# ---------------------------------------------------------------------------


def verify_with_upload(session: Session, campaign: Campaign, application_id: uuid.UUID, raw: bytes,
                       filename: Optional[str], actor: str) -> Dict[str, Any]:
    """Compare a fresh access list for one application against its open remediations.

    A revoke is verified when the account is gone, disabled, or no longer holds the
    entitlement ("Account access" needs the account gone or disabled). A modify is
    verified when the original entitlement is no longer held. Anything still present
    stays open and is reported.
    """
    _owner(campaign, actor, "verify remediation")
    _require_remediating(campaign)
    app = session.get(Application, application_id)
    if app is None:
        raise LookupError("application not found")
    _check_size(raw, filename)
    mappings: List[ColumnMapping] = []
    df = map_columns(_read_table(raw, filename), ACCESS_LIST_ALIASES, "verification", mappings)
    if not ({"native_id", "username", "email"} & set(df.columns)):
        raise IngestError("access list needs a user ID, username or email column")

    present: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        status = _clean(row.get("status"))
        active = True if status is None else _normalize_status_value(status) == "active"
        role = (_clean(row.get("role")) or "").lower()
        keys = {(_clean(row.get(c)) or "").lower() for c in ("native_id", "username", "email")} - {""}
        for k in keys:
            entry = present.setdefault(k, {"active": False, "roles": set()})
            entry["active"] = entry["active"] or active
            if role and active:
                entry["roles"].add(role)

    snap = _snapshot(session, campaign, SourceKind.verification, raw, filename, actor, df, mappings, app)

    results = []
    for r in rows(session, campaign):
        if r["application_id"] != app.id or r["status"] not in OPEN_STATES:
            continue
        rem = session.get(Remediation, r["id"])
        acct = session.scalar(select(Account).join(AccountEntitlement, AccountEntitlement.account_id == Account.id)
                              .join(ReviewItem, ReviewItem.account_entitlement_id == AccountEntitlement.id)
                              .where(ReviewItem.id == rem.review_item_id))
        keys = {k.lower() for k in (acct.native_id, acct.username, acct.email) if k}
        entry = next((present[k] for k in keys if k in present), None)
        ent = (r["entitlement"] or "").lower()
        if entry is None or not entry["active"]:
            gone, why = True, "account not in the new list or disabled"
        elif ent == "account access":
            gone, why = False, "account is still active"
        else:
            gone = ent not in entry["roles"]
            why = "entitlement no longer held" if gone else "entitlement still held"
        if gone:
            rem.status = "verified"
            rem.verified_snapshot_id = snap.id
            rem.verified_at = datetime.now(timezone.utc)
            rem.verification_method = "reupload"
        results.append({"remediation_id": str(rem.id), "account": r["account"], "entitlement": r["entitlement"],
                        "action": r["action"], "verified": gone, "detail": why})
    session.flush()

    verified = sum(1 for x in results if x["verified"])
    record_event(session, campaign, "remediation.verification_upload", {
        "application": app.name, "snapshot_id": snap.id, "sha256": snap.sha256, "filename": filename,
        "row_count": snap.row_count, "checked": len(results), "verified": verified,
        "still_present": [x for x in results if not x["verified"]],
    }, actor)
    return {"application": app.name, "snapshot_id": str(snap.id), "sha256": snap.sha256,
            "checked": len(results), "verified": verified, "results": results}


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


def close_campaign(session: Session, campaign: Campaign, actor: str) -> Dict[str, Any]:
    _owner(campaign, actor, "close the campaign")
    _require_remediating(campaign)
    s = summary(session, campaign)
    if not s["ready_to_close"]:
        pending = s["open"] + s["done"]
        raise RemediationError(f"{pending} remediation(s) still need verifying or accepting before closing")
    transition(session, campaign, CampaignState.verifying, actor)
    transition(session, campaign, CampaignState.closed, actor)
    record_event(session, campaign, "campaign.closed",
                 {"remediations": s["total"], "verified": s["verified"], "accepted": s["accepted"]}, actor)
    return s


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


PRIORITY = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}


def ticket_texts(session: Session, campaign: Campaign) -> List[Dict[str, Any]]:
    """Summary and description for each open remediation, shared by the CSV and the Jira connector."""
    out = []
    for r in rows(session, campaign):
        if r["status"] not in OPEN_STATES:
            continue
        verb = "Remove" if r["action"] == "revoke" else "Change"
        summary_text = f"{verb} {r['entitlement']} for {r['account']} in {r['application']}"
        desc = (f"Access review '{campaign.name}' decided to {r['action']} this access.\n"
                f"Application: {r['application']}\nAccount: {r['account']}\n"
                f"Person: {r['person'] or 'not an employee / service account'}\n"
                f"Entitlement: {r['entitlement']}\nRisk: {r['severity']}\nDecided by: {r['decided_by']}\n"
                + (f"Requested change: {r['change']}\n" if r["action"] == "modify" and r["change"] else "")
                + "When done, record it in UAR Copilot; it will be confirmed against the next access list upload.")
        out.append({"remediation": r, "summary": summary_text, "description": desc,
                    "priority": PRIORITY.get(r["severity"], "Medium")})
    return out


def tickets_csv(session: Session, campaign: Campaign) -> bytes:
    """Open remediations as a CSV that Jira's importer (and most ticket tools) accept."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Summary", "Description", "Issue Type", "Priority", "Labels", "External ID"])
    for t in ticket_texts(session, campaign):
        w.writerow([t["summary"], t["description"], "Task", t["priority"], "access-review",
                    str(t["remediation"]["id"])])
    return buf.getvalue().encode("utf-8")

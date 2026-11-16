"""Emails about a campaign: telling reviewers the review has started, reminding them, escalating.

Reviewers are people from the HR roster. To open the campaign they must be workspace members,
so a reviewer who isn't one yet is added as an invited Reviewer (they join by signing in with
that address). Emails carry counts and a link only, never account names or findings.

Escalation: once the campaign's due date has passed, a reminder to a reviewer with open items
copies that reviewer's manager from the HR roster.

Every send is recorded in the campaign's evidence ledger (who was emailed, how many items,
who was copied, and any failure), so an auditor can see the reviewers were chased.
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import mailer
from backend.db.models import Campaign, CampaignState, Person, ReviewItem, Tenant, WorkspaceMember, WorkspaceRole
from backend.domain.evidence import record_event


class NotificationError(ValueError):
    pass


def _workspace_name(session: Session, tenant_id: uuid.UUID) -> str:
    t = session.get(Tenant, tenant_id)
    return t.name if t else "your workspace"


def ensure_reviewer_member(session: Session, tenant_id: uuid.UUID, email: str, invited_by: str) -> bool:
    """Add `email` to the workspace as an invited Reviewer unless it's already there. True if added."""
    email = email.strip().lower()
    if session.scalar(select(WorkspaceMember.id).where(WorkspaceMember.tenant_id == tenant_id,
                                                       WorkspaceMember.email == email)):
        return False
    session.add(WorkspaceMember(tenant_id=tenant_id, email=email, role=WorkspaceRole.reviewer, status="invited",
                                invited_by=invited_by))
    session.flush()
    return True


def invitation_text(workspace: str, role: str, invited_by: str, email: str) -> tuple[str, str]:
    subject = f"You're invited to {workspace} on UAR Copilot"
    text = (f"{invited_by} invited you to the workspace \"{workspace}\" on UAR Copilot as {role}.\n\n"
            f"To join, sign in with this email address ({email}) and click Join:\n{mailer.app_url('/campaigns')}\n\n"
            "UAR Copilot runs user access reviews: who has access to which systems, and whether they still should.")
    return subject, text


def _load(session: Session, campaign: Campaign) -> Dict[uuid.UUID, Dict[str, int]]:
    load: Dict[uuid.UUID, Counter] = {}
    for item in session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id,
                                                         ReviewItem.reviewer_id.is_not(None))):
        c = load.setdefault(item.reviewer_id, Counter())
        c["total"] += 1
        c["pending"] += item.status == "pending"
    return {k: dict(v) for k, v in load.items()}


def _day(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _due(campaign: Campaign) -> str:
    return f" The review is due {_day(campaign.due_date)}." if campaign.due_date else ""


def _overdue(campaign: Campaign, today: Optional[date] = None) -> bool:
    today = today or datetime.now(timezone.utc).date()
    return bool(campaign.due_date and today > campaign.due_date)


def notify_review_started(session: Session, campaign: Campaign, actor: str) -> Dict[str, Any]:
    """Email every assigned reviewer once the review starts. Never raises for email problems."""
    workspace = _workspace_name(session, campaign.tenant_id)
    link = mailer.app_url(f"/campaigns/{campaign.id}")
    sent, failed, invited = [], [], []
    for person_id, counts in _load(session, campaign).items():
        p = session.get(Person, person_id)
        if p is None or not p.email:
            failed.append({"person_id": str(person_id), "reason": "no email on the HR roster"})
            continue
        email = p.email.lower()
        if ensure_reviewer_member(session, campaign.tenant_id, email, actor):
            invited.append(email)
        text = (f"Hi {p.name or ''},\n\n".replace(" ,", ",") +
                f"{actor} started the access review \"{campaign.name}\" in {workspace}. You're the reviewer for "
                f"{counts['total']} item(s): for each, confirm whether the person still needs that access.{_due(campaign)}\n\n"
                f"Open your items (sign in with {email}):\n{link}\n\n"
                + ("You've been added to the workspace as a Reviewer; click Join after signing in.\n\n"
                   if email in invited else "")
                + "Low-risk items can be kept in bulk; higher-risk ones take a short reason.")
        r = mailer.send([email], f"Access review: {counts['total']} item(s) for you in {campaign.name}", text)
        (sent if r.sent else failed).append({"email": email, "items": counts["total"], **(
            {} if r.sent else {"reason": r.detail})})
    if sent:
        campaign.last_reminder_at = datetime.now(timezone.utc)  # the weekly cadence starts here
    summary = {"emailed": len(sent), "failed": failed, "invited_to_workspace": invited,
               "email_configured": mailer.configured()}
    record_event(session, campaign, "review.reviewers_notified", {**summary, "sent": sent}, actor)
    return summary


def send_reminders(session: Session, campaign: Campaign, actor: str, scheduled: bool = False) -> Dict[str, Any]:
    """Remind reviewers with undecided items. `scheduled` is the weekly job (no signed-in owner)."""
    from backend.domain.review import require_owner

    if not scheduled:
        require_owner(campaign, actor, "send reminders")
    if campaign.state != CampaignState.in_review:
        raise NotificationError("Reminders can only be sent while the review is in progress.")
    if not mailer.configured():
        raise NotificationError("Email isn't set up on this server yet (RESEND_API_KEY), so reminders can't be sent.")
    overdue = _overdue(campaign)
    workspace = _workspace_name(session, campaign.tenant_id)
    link = mailer.app_url(f"/campaigns/{campaign.id}")
    sent, failed, invited = [], [], []
    for person_id, counts in _load(session, campaign).items():
        if not counts.get("pending"):
            continue
        p = session.get(Person, person_id)
        if p is None or not p.email:
            failed.append({"person_id": str(person_id), "reason": "no email on the HR roster"})
            continue
        email = p.email.lower()
        if ensure_reviewer_member(session, campaign.tenant_id, email, actor):
            invited.append(email)
        manager = session.get(Person, p.manager_id) if (overdue and p.manager_id) else None
        cc = [manager.email.lower()] if manager and manager.email else []
        when = (f"It was due {_day(campaign.due_date)} and is now overdue, so your manager is copied."
                if overdue else _due(campaign).strip())
        text = (f"Hi {p.name or ''},\n\n".replace(" ,", ",") +
                f"Reminder: {counts['pending']} of your {counts['total']} item(s) in the access review "
                f"\"{campaign.name}\" ({workspace}) still need a decision. {when}\n\n"
                f"Open your items (sign in with {email}):\n{link}")
        subject = (f"Overdue: {counts['pending']} access review item(s) in {campaign.name}" if overdue
                   else f"Reminder: {counts['pending']} access review item(s) in {campaign.name}")
        r = mailer.send([email], subject, text, cc=cc)
        entry = {"email": email, "pending": counts["pending"], "escalated_to": cc}
        (sent if r.sent else failed).append(entry if r.sent else {**entry, "reason": r.detail})
    summary = {"reminded": len(sent), "escalated": sum(1 for s in sent if s["escalated_to"]), "overdue": overdue,
               "failed": failed, "invited_to_workspace": invited, "scheduled": scheduled}
    if sent:
        campaign.last_reminder_at = datetime.now(timezone.utc)
    record_event(session, campaign, "review.reminders_sent", {**summary, "sent": sent}, actor,
                 actor_kind="system" if scheduled else "human")
    return summary


# ---------------------------------------------------------------------------
# Weekly job
# ---------------------------------------------------------------------------

WEEKLY_ACTOR = "system:weekly-reminders"
MIN_GAP = timedelta(days=6)  # never remind the same campaign twice within this


def set_auto_reminders(session: Session, campaign: Campaign, actor: str, enabled: bool) -> None:
    from backend.domain.review import require_owner

    require_owner(campaign, actor, "change automatic reminders")
    if campaign.auto_reminders != enabled:
        campaign.auto_reminders = enabled
        session.flush()
        record_event(session, campaign, "review.auto_reminders_changed", {"enabled": enabled}, actor)


def run_weekly(engine, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Remind every in-review campaign that has automatic reminders on and wasn't reminded in
    the last six days, across all workspaces. Called by the scheduled job; returns counts only."""
    from sqlalchemy.orm import Session as PlainSession

    from backend.db.session import tenant_session

    if not mailer.configured():
        return {"ran": False, "reason": "email isn't set up on this server"}
    now = now or datetime.now(timezone.utc)
    totals = {"ran": True, "campaigns": 0, "reminded": 0, "escalated": 0, "failed": 0, "errors": 0}
    with PlainSession(engine) as s:
        tenant_ids = list(s.scalars(select(Tenant.id)))  # the tenant table has no row-level security
    for tenant_id in tenant_ids:
        with tenant_session(tenant_id, engine) as session:
            due = session.scalars(select(Campaign).where(
                Campaign.state == CampaignState.in_review, Campaign.auto_reminders.is_(True),
                (Campaign.last_reminder_at.is_(None)) | (Campaign.last_reminder_at < now - MIN_GAP)))
            for campaign in list(due):
                try:
                    with session.begin_nested():  # one campaign's failure doesn't stop the rest
                        r = send_reminders(session, campaign, WEEKLY_ACTOR, scheduled=True)
                except Exception:
                    logging.getLogger("uar.notifications").exception("weekly reminder failed")
                    totals["errors"] += 1
                    continue
                totals["campaigns"] += 1
                totals["reminded"] += r["reminded"]
                totals["escalated"] += r["escalated"]
                totals["failed"] += len(r["failed"])
    return totals

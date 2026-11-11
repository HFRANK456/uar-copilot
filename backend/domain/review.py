"""Review workflow (FR-13, FR-15, FR-16 groundwork, FR-17 self-review).

Rules enforced here, not just in the UI:
- Review starts only after analysis, and freezes the analysis.
- Decisions are append-only rows; changing a decision adds a new row.
- Keeping high or critical access needs a written justification, and so does
  any "modify" (it has to say what changes).
- Bulk keep is limited to items with no findings or only low findings; high
  and critical items are never bulk-approved.
- Nobody decides on their own access.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

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
    ReviewAction,
    ReviewItem,
)
from backend.domain.campaigns import InvalidTransition, transition
from backend.domain.evidence import record_event

DECIDABLE = {ReviewAction.keep, ReviewAction.revoke, ReviewAction.modify}
JUSTIFY_KEEP = {"critical", "high"}
BULK_SEVERITIES = {"none", "low"}
MIN_JUSTIFICATION = 10


class ReviewError(ValueError):
    pass


class SelfReview(PermissionError):
    pass


def _norm(email: Optional[str]) -> Optional[str]:
    return email.strip().lower() if email else None


def launch_review(session: Session, campaign: Campaign, actor: str) -> Dict[str, int]:
    if campaign.state != CampaignState.analyzing:
        raise InvalidTransition(f"cannot start review from state {campaign.state.value}")
    items = list(session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id)))
    if not items:
        raise ReviewError("run analysis before starting the review")
    transition(session, campaign, CampaignState.in_review, actor)
    counts = {"review_items": len(items), "unassigned": sum(1 for i in items if i.reviewer_id is None)}
    record_event(session, campaign, "review.started", counts, actor)
    return counts


def _subject(session: Session, item: ReviewItem) -> tuple[Account, Entitlement, Optional[Person]]:
    grant = session.get(AccountEntitlement, item.account_entitlement_id)
    account = session.get(Account, grant.account_id)
    entitlement = session.get(Entitlement, grant.entitlement_id)
    link = session.scalar(select(IdentityLink).where(IdentityLink.account_id == account.id))
    person = session.get(Person, link.person_id) if link and link.person_id else None
    return account, entitlement, person


def _load_item(session: Session, campaign: Campaign, item_id: uuid.UUID) -> ReviewItem:
    item = session.scalar(select(ReviewItem).where(ReviewItem.id == item_id, ReviewItem.campaign_id == campaign.id))
    if item is None:
        raise LookupError("review item not found")
    return item


def _require_in_review(campaign: Campaign) -> None:
    if campaign.state != CampaignState.in_review:
        raise InvalidTransition(f"decisions can only be made while the campaign is in review (now {campaign.state.value})")


def decide(
    session: Session,
    campaign: Campaign,
    item_id: uuid.UUID,
    actor: str,
    actor_email: Optional[str],
    action: ReviewAction,
    justification: Optional[str] = None,
    channel: str = "web",
    bulk: bool = False,
) -> Decision:
    _require_in_review(campaign)
    if action not in DECIDABLE:
        raise ReviewError("action must be keep, revoke or modify")
    item = _load_item(session, campaign, item_id)
    account, entitlement, person = _subject(session, item)

    if person is not None and _norm(person.email) and _norm(person.email) == _norm(actor_email):
        raise SelfReview("you cannot review your own access")
    if account.email and _norm(account.email) == _norm(actor_email):
        raise SelfReview("you cannot review your own access")

    justification = (justification or "").strip() or None
    if action == ReviewAction.keep and item.severity in JUSTIFY_KEEP and (
        not justification or len(justification) < MIN_JUSTIFICATION
    ):
        raise ReviewError(f"keeping {item.severity}-risk access needs a justification of at least {MIN_JUSTIFICATION} characters")
    if action == ReviewAction.modify and (not justification or len(justification) < MIN_JUSTIFICATION):
        raise ReviewError("say what should change (at least 10 characters)")

    previous = item.status
    decision = Decision(
        tenant_id=campaign.tenant_id,
        review_item_id=item.id,
        actor_id=actor,
        action=action,
        justification=justification,
        channel=channel,
        created_by=actor,
    )
    session.add(decision)
    item.status = action.value
    session.flush()

    record_event(
        session,
        campaign,
        "review.decided",
        {
            "review_item_id": item.id,
            "decision_id": decision.id,
            "action": action.value,
            "previous_status": previous,
            "justification": justification,
            "severity": item.severity,
            "entitlement": entitlement.name,
            "account": account.native_id,
            "person": (person.email or person.name) if person else None,
            "finding_ids": item.finding_ids,
            "bulk": bulk,
            "channel": channel,
        },
        actor,
    )
    return decision


def bulk_keep(
    session: Session, campaign: Campaign, item_ids: Iterable[uuid.UUID], actor: str, actor_email: Optional[str]
) -> Dict[str, object]:
    """Keep many low-risk items at once. Refuses the whole batch if any item isn't eligible."""
    _require_in_review(campaign)
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        raise ReviewError("no items given")
    items = [_load_item(session, campaign, i) for i in ids]
    blocked = [str(i.id) for i in items if i.severity not in BULK_SEVERITIES]
    if blocked:
        raise ReviewError(f"{len(blocked)} item(s) have medium or higher findings and must be decided one at a time")
    decided, skipped_self = 0, []
    for item in items:
        try:
            decide(session, campaign, item.id, actor, actor_email, ReviewAction.keep, bulk=True)
            decided += 1
        except SelfReview:
            skipped_self.append(str(item.id))
    return {"decided": decided, "skipped_own_access": skipped_self}


def reassign(session: Session, campaign: Campaign, item_id: uuid.UUID, person_id: uuid.UUID, actor: str) -> ReviewItem:
    if campaign.state not in (CampaignState.analyzing, CampaignState.in_review):
        raise InvalidTransition(f"cannot reassign in state {campaign.state.value}")
    item = _load_item(session, campaign, item_id)
    reviewer = session.get(Person, person_id)
    if reviewer is None:
        raise LookupError("person not found")
    _, _, subject = _subject(session, item)
    if subject is not None and subject.id == reviewer.id:
        raise SelfReview("a person cannot review their own access")
    previous = item.reviewer_id
    item.reviewer_id = reviewer.id
    session.flush()
    record_event(
        session, campaign, "review.reassigned",
        {"review_item_id": item.id, "from": previous, "to": reviewer.id, "to_email": reviewer.email}, actor,
    )
    return item


def set_app_owner(session: Session, campaign: Campaign, application_id: uuid.UUID, person_id: Optional[uuid.UUID],
                  actor: str) -> Dict[str, object]:
    """Set an application's owner and hand them that app's unassigned items (never their own)."""
    app = session.get(Application, application_id)
    if app is None:
        raise LookupError("application not found")
    owner = session.get(Person, person_id) if person_id else None
    if person_id and owner is None:
        raise LookupError("person not found")
    app.owner_person_id = owner.id if owner else None

    assigned = 0
    if owner is not None and campaign.state in (CampaignState.analyzing, CampaignState.in_review):
        items = session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id,
                                                         ReviewItem.reviewer_id.is_(None)))
        for item in items:
            account, _, subject = _subject(session, item)
            if account.application_id == app.id and (subject is None or subject.id != owner.id):
                item.reviewer_id = owner.id
                assigned += 1
    session.flush()
    record_event(session, campaign, "application.owner_set",
                 {"application": app.name, "owner": owner.email if owner else None, "items_assigned": assigned}, actor)
    return {"application": app.name, "items_assigned": assigned}


def latest_decisions(session: Session, item_ids: List[uuid.UUID]) -> Dict[uuid.UUID, Decision]:
    out: Dict[uuid.UUID, Decision] = {}
    for d in session.scalars(select(Decision).where(Decision.review_item_id.in_(item_ids)).order_by(Decision.decided_at)):
        out[d.review_item_id] = d  # later rows win
    return out


def review_progress(session: Session, campaign: Campaign) -> Dict[str, object]:
    items = list(session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id)))
    status = Counter(i.status for i in items)
    total = len(items)
    pending = status.get("pending", 0)
    high_pending = sum(1 for i in items if i.status == "pending" and i.severity in JUSTIFY_KEEP)
    return {
        "total": total,
        "decided": total - pending,
        "pending": pending,
        "high_risk_pending": high_pending,
        "by_action": {a: status.get(a, 0) for a in ("keep", "revoke", "modify")},
        "unassigned": sum(1 for i in items if i.reviewer_id is None),
        "complete": total > 0 and pending == 0,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

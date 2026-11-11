"""Campaign lifecycle: Draft -> Ingesting -> Resolving identities -> Analyzing ->
In review -> Remediating -> Verifying -> Closed."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import Campaign, CampaignState
from backend.domain.evidence import record_event
from backend.rule_registry import RULE_SET_VERSION

S = CampaignState
ALLOWED_TRANSITIONS: dict[CampaignState, set[CampaignState]] = {
    S.draft: {S.ingesting},
    S.ingesting: {S.resolving_identities},
    S.resolving_identities: {S.analyzing, S.ingesting},
    S.analyzing: {S.in_review, S.ingesting},
    S.in_review: {S.remediating, S.closed},
    S.remediating: {S.verifying},
    S.verifying: {S.closed, S.remediating},
    S.closed: set(),
}


class InvalidTransition(ValueError):
    pass


class NotFound(LookupError):
    pass


def create_campaign(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    name: str,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    due_date: Optional[date] = None,
    reviewer_rule: str = "manager",
) -> Campaign:
    if period_start and period_end and period_end < period_start:
        raise ValueError("period_end is before period_start")
    campaign = Campaign(
        tenant_id=tenant_id,
        name=name,
        period_start=period_start,
        period_end=period_end,
        due_date=due_date,
        reviewer_rule=reviewer_rule,
        rule_set_version=RULE_SET_VERSION,
        state=S.draft,
        created_by=actor,
    )
    session.add(campaign)
    session.flush()
    record_event(
        session,
        campaign,
        "campaign.created",
        {
            "name": name,
            "period_start": period_start,
            "period_end": period_end,
            "due_date": due_date,
            "reviewer_rule": reviewer_rule,
            "rule_set_version": RULE_SET_VERSION,
        },
        actor,
    )
    return campaign


def get_campaign(session: Session, campaign_id: uuid.UUID) -> Campaign:
    # RLS already limits this to the session's tenant; a foreign id reads as not found.
    campaign = session.scalar(select(Campaign).where(Campaign.id == campaign_id))
    if campaign is None:
        raise NotFound(f"campaign {campaign_id} not found")
    return campaign


def transition(session: Session, campaign: Campaign, to: CampaignState, actor: str, actor_kind: str = "human") -> Campaign:
    if to == campaign.state:
        return campaign
    if to not in ALLOWED_TRANSITIONS[campaign.state]:
        raise InvalidTransition(f"cannot move campaign from {campaign.state.value} to {to.value}")
    record_event(session, campaign, "campaign.state_changed", {"from": campaign.state.value, "to": to.value}, actor, actor_kind)
    campaign.state = to
    session.flush()
    return campaign

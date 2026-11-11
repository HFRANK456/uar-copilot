"""Append-only, hash-chained evidence ledger (one chain per campaign).

hash = sha256(prev_hash + canonical JSON of the event). Changing, removing or
reordering any event breaks every hash after it, so an auditor can verify the
whole trail from the stored rows alone.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.models import Campaign, EvidenceEvent

GENESIS_HASH = "0" * 64


def _canonical(event: Dict[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _event_body(campaign_id: uuid.UUID, seq: int, type_: str, payload: Any, actor: str, actor_kind: str, occurred_at: datetime) -> Dict[str, Any]:
    return {
        "campaign_id": str(campaign_id),
        "seq": seq,
        "type": type_,
        "payload": payload,
        "actor": actor,
        "actor_kind": actor_kind,
        "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
    }


def compute_hash(prev_hash: str, body: Dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + _canonical(body)).hexdigest()


def record_event(
    session: Session,
    campaign: Campaign,
    type_: str,
    payload: Dict[str, Any],
    actor: str,
    actor_kind: str = "human",
) -> EvidenceEvent:
    # Lock the campaign row so concurrent writers append in a single order.
    session.execute(select(Campaign.id).where(Campaign.id == campaign.id).with_for_update())
    last = session.execute(
        select(EvidenceEvent.seq, EvidenceEvent.hash)
        .where(EvidenceEvent.campaign_id == campaign.id)
        .order_by(EvidenceEvent.seq.desc())
        .limit(1)
    ).first()
    seq = (last.seq + 1) if last else 1
    prev_hash = last.hash if last else GENESIS_HASH
    occurred_at = datetime.now(timezone.utc)
    # Round-trip through JSON so the stored payload is exactly what was hashed.
    payload = json.loads(json.dumps(payload, default=str))
    body = _event_body(campaign.id, seq, type_, payload, actor, actor_kind, occurred_at)
    event = EvidenceEvent(
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        seq=seq,
        type=type_,
        payload=payload,
        actor=actor,
        actor_kind=actor_kind,
        occurred_at=occurred_at,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, body),
        created_by=actor,
    )
    session.add(event)
    session.flush()
    return event


def verify_chain(session: Session, campaign_id: uuid.UUID) -> Dict[str, Any]:
    events: List[EvidenceEvent] = list(
        session.scalars(
            select(EvidenceEvent).where(EvidenceEvent.campaign_id == campaign_id).order_by(EvidenceEvent.seq)
        )
    )
    prev = GENESIS_HASH
    for expected_seq, ev in enumerate(events, start=1):
        body = _event_body(ev.campaign_id, ev.seq, ev.type, ev.payload, ev.actor, ev.actor_kind, ev.occurred_at)
        if ev.seq != expected_seq or ev.prev_hash != prev or compute_hash(prev, body) != ev.hash:
            return {"valid": False, "events": len(events), "broken_at_seq": ev.seq}
        prev = ev.hash
    return {"valid": True, "events": len(events), "head_hash": prev}


def event_count(session: Session, campaign_id: uuid.UUID) -> int:
    return session.scalar(select(func.count()).select_from(EvidenceEvent).where(EvidenceEvent.campaign_id == campaign_id)) or 0

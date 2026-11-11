"""Campaign API (Phase 1): create campaigns, ingest sources, read the evidence trail."""
from __future__ import annotations

import uuid
from typing import Iterator, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import require_auth
from backend.db.models import AccountType, Application, Campaign, EvidenceEvent, IdentityLink, Person, SourceSnapshot
from backend.db.session import get_engine, tenant_session
from backend.domain import campaigns as campaign_service
from backend.domain import identity as identity_service
from backend.domain import ingest as ingest_service
from backend.domain.evidence import verify_chain
from backend.domain.tenants import resolve_tenant
from schemas.user_schema import (
    CampaignCreate,
    CampaignOut,
    ColumnMapping,
    EvidenceEventOut,
    EvidenceOut,
    IdentityCandidateOut,
    IdentityConfirm,
    IdentityItemOut,
    IdentityListOut,
    IngestOut,
    PersonRef,
    SnapshotOut,
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

MAX_UPLOAD_BYTES = ingest_service.MAX_FILE_BYTES


class Ctx:
    def __init__(self, session: Session, tenant_id: uuid.UUID, actor: str):
        self.session = session
        self.tenant_id = tenant_id
        self.actor = actor


def get_ctx(principal: dict = Depends(require_auth)) -> Iterator[Ctx]:
    engine = get_engine()
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Campaigns need a database; DATABASE_URL is not set.")
    try:
        tenant_id = resolve_tenant(engine, principal)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    actor = str(principal.get("email") or principal.get("sub") or "unknown")
    with tenant_session(tenant_id, engine) as session:
        yield Ctx(session, tenant_id, actor)


def _snapshot_out(session: Session, snap: SourceSnapshot) -> SnapshotOut:
    app_name = session.scalar(select(Application.name).where(Application.id == snap.application_id)) if snap.application_id else None
    return SnapshotOut(
        id=snap.id,
        kind=snap.kind.value,
        application=app_name,
        filename=snap.filename,
        sha256=snap.sha256,
        size_bytes=snap.size_bytes,
        row_count=snap.row_count,
        column_mapping=[ColumnMapping(**m) for m in snap.column_mapping or []],
        needs_confirmation=ingest_service.snapshot_needs_confirmation(snap),
        mapping_confirmed_by=snap.mapping_confirmed_by,
        created_at=snap.created_at,
    )


def _campaign_out(session: Session, c: Campaign, with_snapshots: bool = False) -> CampaignOut:
    snaps: List[SnapshotOut] = []
    if with_snapshots:
        rows = session.scalars(
            select(SourceSnapshot).where(SourceSnapshot.campaign_id == c.id).order_by(SourceSnapshot.created_at)
        )
        snaps = [_snapshot_out(session, s) for s in rows]
    return CampaignOut(
        id=c.id,
        name=c.name,
        state=c.state.value,
        period_start=c.period_start,
        period_end=c.period_end,
        due_date=c.due_date,
        reviewer_rule=c.reviewer_rule,
        rule_set_version=c.rule_set_version,
        created_by=c.created_by,
        created_at=c.created_at,
        snapshots=snaps,
    )


def _load(ctx: Ctx, campaign_id: uuid.UUID) -> Campaign:
    try:
        return campaign_service.get_campaign(ctx.session, campaign_id)
    except campaign_service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found") from exc


def _read_upload(upload: UploadFile) -> bytes:
    raw = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File too large: {upload.filename}")
    return raw


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(body: CampaignCreate, ctx: Ctx = Depends(get_ctx)):
    try:
        c = campaign_service.create_campaign(ctx.session, ctx.tenant_id, ctx.actor, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _campaign_out(ctx.session, c)


@router.get("", response_model=List[CampaignOut])
def list_campaigns(ctx: Ctx = Depends(get_ctx)):
    rows = ctx.session.scalars(select(Campaign).order_by(Campaign.created_at.desc()))
    return [_campaign_out(ctx.session, c) for c in rows]


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    return _campaign_out(ctx.session, _load(ctx, campaign_id), with_snapshots=True)


@router.post("/{campaign_id}/sources/hr", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
def ingest_hr(campaign_id: uuid.UUID, file: UploadFile = File(...), ctx: Ctx = Depends(get_ctx)):
    raw = _read_upload(file)
    c = _load(ctx, campaign_id)
    try:
        result = ingest_service.ingest_hr_roster(ctx.session, c, raw, file.filename, ctx.actor)
    except campaign_service.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ingest_service.IngestError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return IngestOut(
        snapshot=_snapshot_out(ctx.session, result.snapshot),
        counts=result.counts,
        warnings=result.warnings,
        campaign_state=c.state.value,
    )


@router.post("/{campaign_id}/sources/access", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
def ingest_access(
    campaign_id: uuid.UUID,
    application: str = Form(...),
    file: UploadFile = File(...),
    ctx: Ctx = Depends(get_ctx),
):
    raw = _read_upload(file)
    c = _load(ctx, campaign_id)
    try:
        result = ingest_service.ingest_access_list(ctx.session, c, application, raw, file.filename, ctx.actor)
    except campaign_service.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ingest_service.IngestError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return IngestOut(
        snapshot=_snapshot_out(ctx.session, result.snapshot),
        counts=result.counts,
        warnings=result.warnings,
        campaign_state=c.state.value,
    )


@router.post("/{campaign_id}/sources/{snapshot_id}/confirm-mapping", response_model=SnapshotOut)
def confirm_mapping(campaign_id: uuid.UUID, snapshot_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    c = _load(ctx, campaign_id)
    try:
        snap = ingest_service.confirm_mapping(ctx.session, c, snapshot_id, ctx.actor)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot not found") from exc
    return _snapshot_out(ctx.session, snap)


@router.get("/{campaign_id}/evidence", response_model=EvidenceOut)
def get_evidence(campaign_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    c = _load(ctx, campaign_id)
    events = ctx.session.scalars(
        select(EvidenceEvent).where(EvidenceEvent.campaign_id == c.id).order_by(EvidenceEvent.seq)
    )
    return EvidenceOut(
        campaign_id=c.id,
        verification=verify_chain(ctx.session, c.id),
        events=[
            EvidenceEventOut(
                seq=e.seq,
                type=e.type,
                actor=e.actor,
                actor_kind=e.actor_kind,
                occurred_at=e.occurred_at,
                payload=e.payload,
                prev_hash=e.prev_hash,
                hash=e.hash,
            )
            for e in events
        ],
    )


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def _person_ref(p: Person) -> PersonRef:
    return PersonRef(
        id=p.id,
        employee_id=p.employee_id,
        name=p.name,
        email=p.email,
        department=p.department,
        employment_status=p.employment_status,
    )


def _identity_list(ctx: Ctx, c: Campaign, status_filter: str | None = None) -> IdentityListOut:
    accounts = identity_service.campaign_accounts(ctx.session, c)
    ids = [a.id for a in accounts]
    links = {
        link.account_id: link
        for link in ctx.session.scalars(select(IdentityLink).where(IdentityLink.account_id.in_(ids)))
    }
    people = {p.id: p for p in ctx.session.scalars(select(Person))}
    apps = {a.id: a.name for a in ctx.session.scalars(select(Application))}
    items = []
    for a in sorted(accounts, key=lambda a: (apps.get(a.application_id) or "", a.native_id)):
        link = links.get(a.id)
        status_value = link.status if link else "not_resolved"
        if status_filter and status_value != status_filter:
            continue
        cands = []
        for cand in (link.candidates if link else []) or []:
            person = people.get(uuid.UUID(cand["person_id"]))
            if person:
                cands.append(IdentityCandidateOut(person=_person_ref(person), method=cand["method"], confidence=cand["confidence"]))
        person = people.get(link.person_id) if link and link.person_id else None
        items.append(
            IdentityItemOut(
                account_id=a.id,
                application=apps.get(a.application_id),
                native_id=a.native_id,
                username=a.username,
                email=a.email,
                display_name=a.display_name,
                account_type=a.account_type.value,
                status=status_value,
                method=link.method if link else None,
                confidence=link.confidence if link else None,
                person=_person_ref(person) if person else None,
                confirmed_by=link.confirmed_by if link else None,
                candidates=cands,
            )
        )
    return IdentityListOut(summary=identity_service.identity_summary(ctx.session, c), items=items)


@router.post("/{campaign_id}/identities/resolve", response_model=IdentityListOut)
def resolve_identities(campaign_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    c = _load(ctx, campaign_id)
    try:
        identity_service.resolve_identities(ctx.session, c, ctx.actor)
    except campaign_service.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _identity_list(ctx, c)


@router.get("/{campaign_id}/identities", response_model=IdentityListOut)
def list_identities(campaign_id: uuid.UUID, status_filter: str | None = None, ctx: Ctx = Depends(get_ctx)):
    return _identity_list(ctx, _load(ctx, campaign_id), status_filter)


@router.post("/{campaign_id}/identities/{account_id}", response_model=IdentityItemOut)
def confirm_identity(campaign_id: uuid.UUID, account_id: uuid.UUID, body: IdentityConfirm, ctx: Ctx = Depends(get_ctx)):
    c = _load(ctx, campaign_id)
    try:
        identity_service.confirm_link(
            ctx.session,
            c,
            account_id,
            ctx.actor,
            person_id=body.person_id,
            account_type=AccountType(body.account_type) if body.account_type else None,
        )
    except campaign_service.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return next(i for i in _identity_list(ctx, c).items if i.account_id == account_id)

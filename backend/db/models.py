"""Data model from the PRD.

Every tenant-owned table carries `tenant_id`, `created_at` and `created_by`.
Postgres row-level security (see the initial migration) restricts every query
to the tenant set for the current transaction. `decision` and `evidence_event`
are append-only, enforced by triggers.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TenantOwned:
    """Columns shared by every tenant-scoped table."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[Optional[str]] = mapped_column(String(255))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CampaignState(str, enum.Enum):
    draft = "draft"
    ingesting = "ingesting"
    resolving_identities = "resolving_identities"
    analyzing = "analyzing"
    in_review = "in_review"
    remediating = "remediating"
    verifying = "verifying"
    closed = "closed"


class AccountType(str, enum.Enum):
    human = "human"
    service = "service"
    shared = "shared"
    external = "external"
    unknown = "unknown"


class SourceKind(str, enum.Enum):
    access_list = "access_list"
    hr_roster = "hr_roster"


class ReviewAction(str, enum.Enum):
    keep = "keep"
    revoke = "revoke"
    modify = "modify"
    investigate = "investigate"
    delegate = "delegate"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255))
    # Auth0 Organization id; the token's org_id claim resolves to a tenant.
    auth0_org_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    parent_tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="SET NULL")
    )
    plan: Mapped[str] = mapped_column(String(50), default="design_partner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Identity graph
# ---------------------------------------------------------------------------


class Person(TenantOwned, Base):
    __tablename__ = "person"
    __table_args__ = (UniqueConstraint("tenant_id", "employee_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    employee_id: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(320), index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id", ondelete="SET NULL")
    )
    manager_ref: Mapped[Optional[str]] = mapped_column(String(255))  # raw value from the roster
    department: Mapped[Optional[str]] = mapped_column(String(255))
    title: Mapped[Optional[str]] = mapped_column(String(255))
    employment_status: Mapped[Optional[str]] = mapped_column(String(50))
    hire_date: Mapped[Optional[date]] = mapped_column(Date)
    termination_date: Mapped[Optional[date]] = mapped_column(Date)
    source_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_snapshot.id", ondelete="SET NULL", use_alter=True, name="fk_person_source_snapshot"),
    )


class Application(TenantOwned, Base):
    __tablename__ = "application"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255))
    owner_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id", ondelete="SET NULL")
    )
    criticality: Mapped[Optional[str]] = mapped_column(String(50))
    in_scope_soc2: Mapped[bool] = mapped_column(Boolean, default=True)
    connector_id: Mapped[Optional[str]] = mapped_column(String(255))


class Account(TenantOwned, Base):
    __tablename__ = "account"
    __table_args__ = (UniqueConstraint("tenant_id", "application_id", "source_snapshot_id", "native_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application.id", ondelete="CASCADE"), index=True
    )
    source_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_snapshot.id", ondelete="CASCADE"), index=True
    )
    native_id: Mapped[str] = mapped_column(String(255))
    username: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(320), index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[Optional[str]] = mapped_column(String(50))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    account_type: Mapped[AccountType] = mapped_column(_enum(AccountType, "account_type"), default=AccountType.unknown)
    row_ref: Mapped[Optional[int]] = mapped_column(Integer)  # row in the source snapshot


class IdentityLink(TenantOwned, Base):
    __tablename__ = "identity_link"
    __table_args__ = (UniqueConstraint("account_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"))
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id", ondelete="CASCADE")
    )
    method: Mapped[str] = mapped_column(String(50))  # employee_id | email | username | name | manual | none
    # matched | ambiguous | unmatched | non_human
    status: Mapped[str] = mapped_column(String(20), default="unmatched", server_default="unmatched", index=True)
    # Ranked alternatives shown to a human for ambiguous links: [{person_id, method, confidence}]
    candidates: Mapped[Any] = mapped_column(JsonType, default=list, server_default="[]")
    confidence: Mapped[float] = mapped_column(Float)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(255))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Entitlement(TenantOwned, Base):
    __tablename__ = "entitlement"
    __table_args__ = (UniqueConstraint("tenant_id", "application_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    privilege_tier: Mapped[Optional[str]] = mapped_column(String(20))
    category: Mapped[Optional[str]] = mapped_column(String(100))


class AccountEntitlement(TenantOwned, Base):
    __tablename__ = "account_entitlement"
    __table_args__ = (UniqueConstraint("account_id", "entitlement_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    entitlement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entitlement.id", ondelete="CASCADE")
    )
    granted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


class Campaign(TenantOwned, Base):
    __tablename__ = "campaign"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255))
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[Optional[date]] = mapped_column(Date)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    state: Mapped[CampaignState] = mapped_column(_enum(CampaignState, "campaign_state"), default=CampaignState.draft)
    reviewer_rule: Mapped[str] = mapped_column(String(50), default="manager")  # manager | app_owner | named
    rule_set_version: Mapped[Optional[str]] = mapped_column(String(50))
    signed_off_by: Mapped[Optional[str]] = mapped_column(String(255))
    signed_off_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SourceSnapshot(TenantOwned, Base):
    """An immutable, hashed copy of one ingested file (FR-05)."""

    __tablename__ = "source_snapshot"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    application_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(255))  # "upload" or a connector name
    filename: Mapped[Optional[str]] = mapped_column(String(512))
    file_uri: Mapped[Optional[str]] = mapped_column(String(1024))  # object storage, once on S3
    content: Mapped[Optional[bytes]] = mapped_column(LargeBinary)  # raw bytes until object storage exists
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    column_mapping: Mapped[Any] = mapped_column(JsonType, default=list)
    mapping_confirmed_by: Mapped[Optional[str]] = mapped_column(String(255))
    completeness_status: Mapped[str] = mapped_column(String(50), default="not_checked")


class ReviewItem(TenantOwned, Base):
    """One person's one entitlement in one application during one campaign."""

    __tablename__ = "review_item"
    __table_args__ = (UniqueConstraint("campaign_id", "account_entitlement_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="CASCADE"), index=True
    )
    account_entitlement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account_entitlement.id", ondelete="CASCADE")
    )
    reviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id", ondelete="SET NULL")
    )
    recommendation: Mapped[Optional[ReviewAction]] = mapped_column(_enum(ReviewAction, "review_action"))
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    finding_ids: Mapped[Any] = mapped_column(JsonType, default=list)
    # Highest severity among this item's findings, or "none"; drives queue order and bulk rules.
    severity: Mapped[str] = mapped_column(String(20), default="none", server_default="none", index=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")


class Finding(TenantOwned, Base):
    __tablename__ = "finding"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[str] = mapped_column(String(100))
    rule_version: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(20))
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE")
    )
    inputs: Mapped[Any] = mapped_column(JsonType, default=dict)
    explanation: Mapped[str] = mapped_column(Text)


class Decision(TenantOwned, Base):
    """Append-only: a changed decision is a new row."""

    __tablename__ = "decision"

    id: Mapped[uuid.UUID] = _uuid_pk()
    review_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_item.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[str] = mapped_column(String(255))
    action: Mapped[ReviewAction] = mapped_column(_enum(ReviewAction, "review_action"))
    justification: Mapped[Optional[str]] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    channel: Mapped[str] = mapped_column(String(50), default="web")


class Remediation(TenantOwned, Base):
    __tablename__ = "remediation"

    id: Mapped[uuid.UUID] = _uuid_pk()
    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("decision.id", ondelete="CASCADE"))
    ticket_ref: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="open")
    verified_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_snapshot.id", ondelete="SET NULL")
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class EvidenceEvent(TenantOwned, Base):
    """Append-only, SHA-256 hash-chained per campaign."""

    __tablename__ = "evidence_event"
    __table_args__ = (UniqueConstraint("campaign_id", "seq"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[Any] = mapped_column(JsonType)
    actor: Mapped[str] = mapped_column(String(255))
    actor_kind: Mapped[str] = mapped_column(String(20), default="human")  # human | agent | system
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class Rule(TenantOwned, Base):
    """Per-tenant rule overrides; the default rule set lives in code."""

    __tablename__ = "rule"
    __table_args__ = (UniqueConstraint("tenant_id", "rule_key", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_key: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[Any] = mapped_column(JsonType)
    severity: Mapped[str] = mapped_column(String(20))
    control_refs: Mapped[Any] = mapped_column(JsonType, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


TENANT_TABLES = [
    t.__tablename__
    for t in (
        Person, Application, Account, IdentityLink, Entitlement, AccountEntitlement, Campaign,
        SourceSnapshot, ReviewItem, Finding, Decision, Remediation, EvidenceEvent, Rule,
    )
]
APPEND_ONLY_TABLES = ["decision", "evidence_event"]

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class Issue(BaseModel):
    type: str
    severity: str
    explanation: str
    rule_version: int = 1
    inputs: Dict[str, Any] = Field(default_factory=dict)
    row_ref: Optional[Union[int, str]] = None
    control_refs: List[str] = Field(default_factory=list)


class Finding(BaseModel):
    user_id: str
    severity: str
    issues: List[Issue]


class Summary(BaseModel):
    critical: int = 0
    high: int
    medium: int
    low: int
    total: int


class ColumnMapping(BaseModel):
    source: str
    target: str
    matched_column: Optional[str]
    confidence: float
    method: str  # exact | alias | fuzzy | none
    needs_confirmation: bool = False


class InputDigest(BaseModel):
    source: str
    filename: Optional[str]
    sha256: str
    row_count: int


class UploadResponse(BaseModel):
    summary: Summary
    findings: List[Finding]
    column_mappings: List[ColumnMapping] = Field(default_factory=list)
    inputs: List[InputDigest] = Field(default_factory=list)
    rule_set_version: Optional[str] = None
    evaluated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Campaigns (Phase 1)
# ---------------------------------------------------------------------------

from datetime import date as _date, datetime as _datetime  # noqa: E402
from uuid import UUID as _UUID  # noqa: E402


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    period_start: Optional[_date] = None
    period_end: Optional[_date] = None
    due_date: Optional[_date] = None
    reviewer_rule: str = Field(default="manager", pattern="^(manager|app_owner|named)$")


class SnapshotOut(BaseModel):
    id: _UUID
    kind: str
    application: Optional[str] = None
    filename: Optional[str]
    sha256: str
    size_bytes: int
    row_count: int
    column_mapping: List[ColumnMapping]
    needs_confirmation: bool
    mapping_confirmed_by: Optional[str]
    created_at: Optional[_datetime] = None


class CampaignOut(BaseModel):
    id: _UUID
    name: str
    state: str
    period_start: Optional[_date]
    period_end: Optional[_date]
    due_date: Optional[_date]
    reviewer_rule: str
    rule_set_version: Optional[str]
    created_by: Optional[str]
    created_at: Optional[_datetime]
    snapshots: List[SnapshotOut] = Field(default_factory=list)


class IngestOut(BaseModel):
    snapshot: SnapshotOut
    counts: Dict[str, int]
    warnings: List[str]
    campaign_state: str


class EvidenceEventOut(BaseModel):
    seq: int
    type: str
    actor: str
    actor_kind: str
    occurred_at: _datetime
    payload: Any
    prev_hash: str
    hash: str


class EvidenceOut(BaseModel):
    campaign_id: _UUID
    verification: Dict[str, Any]
    events: List[EvidenceEventOut]


class PersonRef(BaseModel):
    id: _UUID
    employee_id: Optional[str]
    name: Optional[str]
    email: Optional[str]
    department: Optional[str]
    employment_status: Optional[str]


class IdentityCandidateOut(BaseModel):
    person: PersonRef
    method: str
    confidence: float


class IdentityItemOut(BaseModel):
    account_id: _UUID
    application: Optional[str]
    native_id: str
    username: Optional[str]
    email: Optional[str]
    display_name: Optional[str]
    account_type: str
    status: str
    method: Optional[str]
    confidence: Optional[float]
    person: Optional[PersonRef]
    confirmed_by: Optional[str]
    candidates: List[IdentityCandidateOut]


class IdentityListOut(BaseModel):
    summary: Dict[str, Any]
    items: List[IdentityItemOut]


class IdentityConfirm(BaseModel):
    person_id: Optional[_UUID] = None
    account_type: Optional[str] = Field(default=None, pattern="^(service|shared|external)$")

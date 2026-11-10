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

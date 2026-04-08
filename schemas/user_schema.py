from __future__ import annotations

from typing import List

from pydantic import BaseModel


class Issue(BaseModel):
    type: str
    severity: str
    explanation: str


class Finding(BaseModel):
    user_id: str
    severity: str
    issues: List[Issue]


class Summary(BaseModel):
    high: int
    medium: int
    low: int
    total: int


class UploadResponse(BaseModel):
    summary: Summary
    findings: List[Finding]

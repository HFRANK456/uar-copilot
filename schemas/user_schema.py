from __future__ import annotations

from typing import List

from pydantic import BaseModel


class AggregatedUser(BaseModel):
    user_id: str
    issues: List[str]
    severity: str
    explanations: List[str]

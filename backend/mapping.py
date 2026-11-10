"""Column mapping for messy exports: exact, alias, then fuzzy match, with every decision recorded."""
from __future__ import annotations

import difflib
import re
import logging
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from schemas.user_schema import ColumnMapping

logger = logging.getLogger(__name__)

MAPPING_CONFIDENCE_THRESHOLD = 0.85
FUZZY_THRESHOLD = 0.75

ACCESS_LIST_ALIASES: Dict[str, List[str]] = {
    "native_id": ["user_id", "uid", "id", "account_id", "login", "user", "employee_id"],
    "username": ["username", "user_name", "login_name", "upn", "user_principal_name", "samaccountname"],
    "email": ["email", "email_address", "mail", "work_email", "primary_email"],
    "display_name": ["display_name", "name", "full_name"],
    "status": ["status", "account_status", "enabled", "active_flag", "acct_status", "state"],
    "role": ["role", "access_role", "permission", "permission_set", "user_role", "group", "entitlement", "access_level"],
    "last_login": ["last_login", "last_login_at", "last_sign_in", "last_signin", "last_activity", "last_seen"],
}

HR_ROSTER_ALIASES: Dict[str, List[str]] = {
    "employee_id": ["employee_id", "emp_id", "employee_number", "worker_id", "employee_no", "id"],
    "email": ["email", "work_email", "email_address", "mail", "primary_email"],
    "name": ["name", "full_name", "employee_name", "display_name", "legal_name"],
    "manager": ["manager", "manager_id", "manager_email", "supervisor", "reports_to", "manager_employee_id"],
    "department": ["department", "dept", "team", "org_unit", "division"],
    "title": ["title", "job_title", "position", "role"],
    "status": ["status", "employment_status", "employee_status", "worker_status"],
    "hire_date": ["hire_date", "start_date", "date_of_hire", "hired_at"],
    "termination_date": ["termination_date", "end_date", "terminated_at", "term_date", "last_day"],
}


def normalize_header(name: object) -> str:
    """'Emp ID' -> 'emp_id', 'Last-Sign-In' -> 'last_sign_in'."""
    return re.sub(r"[\s\-/.]+", "_", str(name).strip().lower()).strip("_")


def find_best_column(
    columns: Sequence[str], target: str, aliases: Sequence[str], threshold: float = FUZZY_THRESHOLD
) -> Tuple[Optional[str], float, str]:
    """Return (column, confidence, method) for the best match, or (None, 0.0, 'none')."""
    columns = list(columns)
    if target in columns:
        return target, 1.0, "exact"
    for alias in aliases:  # alias order is priority order
        if alias in columns:
            return alias, 0.99, "alias"

    best_match, best_score = None, 0.0
    for col in columns:
        for alias in [target, *aliases]:
            score = difflib.SequenceMatcher(None, col, alias).ratio()
            if score > best_score:
                best_score, best_match = score, col
    if best_score >= threshold:
        return best_match, best_score, "fuzzy"
    return None, 0.0, "none"


def map_columns(
    df: pd.DataFrame,
    aliases: Dict[str, List[str]],
    source: str,
    mapping_log: Optional[List[ColumnMapping]] = None,
) -> pd.DataFrame:
    """Rename columns to canonical targets. Each source column satisfies at most one target."""
    normalized = df.copy()
    normalized.columns = [normalize_header(c) for c in normalized.columns]

    used: set[str] = set()
    rename: Dict[str, str] = {}
    for target, target_aliases in aliases.items():
        available = [c for c in normalized.columns if c not in used]
        match, score, method = find_best_column(available, target, target_aliases)
        if match is not None:
            used.add(match)
            if match != target:
                rename[match] = target
        entry = ColumnMapping(
            source=source,
            target=target,
            matched_column=match,
            confidence=round(float(score), 2),
            method=method,
            needs_confirmation=match is not None and score < MAPPING_CONFIDENCE_THRESHOLD,
        )
        if entry.needs_confirmation:
            logger.warning(
                "weak_column_match",
                extra={"source": source, "target": target, "column": match, "confidence": entry.confidence},
            )
        if mapping_log is not None:
            mapping_log.append(entry)

    # Drop any canonical-named column that would collide with a rename target.
    collisions = [c for c in normalized.columns if c in rename.values() and c not in rename]
    normalized = normalized.drop(columns=collisions).rename(columns=rename)
    logger.info("column_mapping_applied", extra={"source": source, "rename_map": rename})
    return normalized

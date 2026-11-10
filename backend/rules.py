from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, List, Mapping, Optional

import numpy as np
import pandas as pd

from backend.mapping import find_best_column, map_columns  # noqa: F401 (re-exported)
from backend.privileged_access_catalog import classify_privileged_access
from backend.rule_registry import RULES
from backend.utils import require_columns
from schemas.user_schema import ColumnMapping, Finding, Issue

logger = logging.getLogger(__name__)

SEVERITY_PRIORITY = {"critical": 4, "high": 3, "medium": 2, "low": 1}

COLUMN_ALIASES: dict[str, list[str]] = {
    "user_id": ["user_id", "uid", "id", "employee_id", "user"],
    "status": ["status", "account_status", "enabled", "active_flag", "acct_status"],
    "role": ["role", "access_role", "permission", "user_role", "group"],
    "termination_date": ["termination_date", "end_date", "terminated_at"],
}

ACTIVE_VALUES = {"active", "enabled", "true", "1", "yes", "y"}
INACTIVE_VALUES = {"inactive", "disabled", "false", "0", "no", "n"}


def termination_grace_business_days() -> int:
    try:
        return max(0, int(os.getenv("TERMINATION_GRACE_BUSINESS_DAYS", "1")))
    except ValueError:
        return 1


# --------------------------------------------------------------------------
# Column mapping
# --------------------------------------------------------------------------


def normalize_columns(
    df: pd.DataFrame,
    source: str = "input",
    mapping_log: Optional[List[ColumnMapping]] = None,
) -> pd.DataFrame:
    """Rename messy export headers to canonical names and record every decision."""
    normalized = map_columns(df, COLUMN_ALIASES, source, mapping_log)

    if "user_id" not in normalized.columns:
        require_columns(normalized, ["user_id"], source)

    if "role" not in normalized.columns:
        logger.warning("role_column_missing", extra={"source": source})
        normalized["role"] = "unknown"

    return normalized


def _normalize_status_value(val: object) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "inactive"
    v = str(val).strip().lower()
    if v in ACTIVE_VALUES:
        return "active"
    if v in INACTIVE_VALUES:
        return "inactive"
    return v  # keep unexpected values visible


def normalize_values(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized["user_id"] = normalized["user_id"].astype(str).str.strip()
    if "status" in normalized.columns:
        normalized["status"] = normalized["status"].apply(_normalize_status_value)
    if "role" in normalized.columns:
        normalized["role"] = normalized["role"].fillna("").astype(str).str.strip().str.lower()
    return normalized


# --------------------------------------------------------------------------
# Feature flags
# --------------------------------------------------------------------------


def _classify(role: str) -> tuple[bool, str, str, str]:
    match = classify_privileged_access(role)
    if match.is_privileged:
        return True, match.privilege_category, match.risk_tier, match.matched_pattern
    if "admin" in role:
        # Looks administrative but not catalogued: surface it at low severity.
        return True, "unclassified_admin", "low", "admin"
    return False, "standard_access", "none", ""


def compute_flags(df: pd.DataFrame, as_of: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    df = df.copy()
    today = (as_of or pd.Timestamp.today()).normalize()

    df["status"] = df.get("status", pd.Series(index=df.index, dtype=object)).apply(_normalize_status_value)
    df["role"] = df.get("role", pd.Series(index=df.index, dtype=object)).fillna("").astype(str).str.strip().str.lower()

    if "termination_date" in df.columns:
        df["termination_date"] = pd.to_datetime(df["termination_date"], errors="coerce")
    else:
        df["termination_date"] = pd.NaT

    df["is_active"] = df["status"] == "active"

    classified = df["role"].apply(_classify)
    df["is_privileged"] = classified.str[0].astype(bool)
    df["privilege_category"] = classified.str[1]
    df["privilege_tier"] = classified.str[2]
    df["privilege_match"] = classified.str[3]
    df["is_admin"] = df["is_privileged"]  # backwards-compatible alias

    # A future-dated termination is scheduled, not yet effective.
    df["is_terminated"] = df["termination_date"].notna() & (df["termination_date"] <= today)

    df["days_since_termination"] = (today - df["termination_date"]).dt.days.where(df["is_terminated"], 0).fillna(0).astype(int)

    grace = termination_grace_business_days()

    def _business_days(ts) -> int:
        if pd.isna(ts) or ts > today:
            return 0
        return int(np.busday_count(ts.date(), today.date()))

    df["business_days_since_termination"] = df["termination_date"].apply(_business_days)
    df["within_termination_grace"] = df["is_terminated"] & (df["business_days_since_termination"] <= grace)

    return df


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------


def _compare(actual: Any, op: str, value: Any) -> bool:
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    if actual is None or (not isinstance(actual, (list, dict, str)) and pd.isna(actual)):
        return False
    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    raise ValueError(f"Unsupported comparison operator: {op}")


def evaluate_condition(row: Mapping[str, Any], cond: Mapping[str, Any], as_of: Optional[pd.Timestamp] = None) -> bool:
    field = cond["field"]
    op = cond["operator"]
    value = cond.get("value")
    actual = row.get(field) if hasattr(row, "get") else row[field]

    if op in {"==", "!=", ">", ">=", "<", "<="}:
        return bool(_compare(actual, op, value))
    if op == "in":
        return actual in set(value)
    if op == "not_in":
        return actual not in set(value)
    if op == "contains":
        return actual is not None and str(value).lower() in str(actual).lower()
    if op == "regex":
        return actual is not None and re.search(str(value), str(actual), re.IGNORECASE) is not None
    if op == "days_since":
        ts = pd.to_datetime(actual, errors="coerce")
        if pd.isna(ts):
            return False
        today = (as_of or pd.Timestamp.today()).normalize()
        return bool(_compare((today - ts).days, value["op"], value["days"]))

    raise ValueError(f"Unsupported operator: {op}")


def _describe(row: Mapping[str, Any], field: str) -> str:
    actual = row.get(field)
    if field in {"is_admin", "is_privileged", "privilege_tier", "privilege_category"}:
        role_val = row.get("role")
        if role_val:
            return f"role='{role_val}' -> {field}={actual}"
    if field == "is_active":
        status_val = row.get("status")
        if status_val:
            return f"status='{status_val}' -> is_active={actual}"
    if field in {"is_terminated", "within_termination_grace"}:
        term_val = row.get("termination_date")
        if term_val is not None and not pd.isna(term_val):
            extra = ""
            if field == "within_termination_grace":
                extra = f", business_days_since_termination={row.get('business_days_since_termination')}"
            return f"termination_date='{pd.Timestamp(term_val).date()}'{extra} -> {field}={actual}"
    return f"{field}={actual}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if value is None or (not isinstance(value, (str, list, dict)) and pd.isna(value)):
        return None
    return value


def evaluate_rules(df: pd.DataFrame, rules: Iterable[Mapping[str, Any]] = RULES, as_of: Optional[pd.Timestamp] = None):
    results = []
    rules = list(rules)
    for row_index, row in df.iterrows():
        for rule in rules:
            if not all(evaluate_condition(row, cond, as_of) for cond in rule["conditions"]):
                continue
            fields = [c["field"] for c in rule["conditions"]]
            details = [_describe(row, f) for f in fields]
            input_fields = {"user_id", "status", "role", "termination_date", *fields}
            if any(f.startswith("privilege") or f in {"is_admin", "is_privileged"} for f in fields):
                input_fields.update({"privilege_tier", "privilege_category", "privilege_match"})
            results.append(
                {
                    "user_id": row["user_id"],
                    "issue_type": rule["id"],
                    "rule_version": rule.get("version", 1),
                    "severity": rule["severity"],
                    "explanation": f"Rule '{rule['id']}' triggered because " + ", ".join(details),
                    "inputs": {f: _jsonable(row.get(f)) for f in sorted(input_fields)},
                    "row_ref": int(row_index) if isinstance(row_index, (int, np.integer)) else str(row_index),
                    "control_refs": list(rule.get("control_refs", [])),
                }
            )
    return results


def aggregate_results(results) -> List[Finding]:
    aggregated: dict[str, dict[str, Any]] = {}
    for r in results:
        uid = r["user_id"]
        entry = aggregated.setdefault(uid, {"user_id": uid, "severity": r["severity"], "issues": []})
        entry["issues"].append(
            Issue(
                type=r["issue_type"],
                severity=r["severity"],
                explanation=r["explanation"],
                rule_version=r.get("rule_version", 1),
                inputs=r.get("inputs", {}),
                row_ref=r.get("row_ref"),
                control_refs=r.get("control_refs", []),
            )
        )
        if SEVERITY_PRIORITY[r["severity"]] > SEVERITY_PRIORITY[entry["severity"]]:
            entry["severity"] = r["severity"]

    findings = [Finding(**v) for v in aggregated.values()]
    findings.sort(key=lambda f: (-SEVERITY_PRIORITY[f.severity], f.user_id))
    return findings


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def merge_data(
    user_access: pd.DataFrame,
    termination: pd.DataFrame,
    mapping_log: Optional[List[ColumnMapping]] = None,
) -> pd.DataFrame:
    user_access = normalize_values(normalize_columns(user_access, "user_access", mapping_log))
    termination = normalize_values(normalize_columns(termination, "termination", mapping_log))
    require_columns(user_access, ["user_id"], "user_access.csv")
    require_columns(termination, ["user_id", "termination_date"], "termination.csv")

    termination = termination[["user_id", "termination_date"]].drop_duplicates("user_id", keep="last")
    return user_access.merge(termination, on="user_id", how="left")


def flag_users(user_access: pd.DataFrame, termination: pd.DataFrame, as_of: Optional[pd.Timestamp] = None) -> List[Finding]:
    merged = merge_data(user_access, termination)
    evaluation_df = compute_flags(merged, as_of)
    return aggregate_results(evaluate_rules(evaluation_df, as_of=as_of))

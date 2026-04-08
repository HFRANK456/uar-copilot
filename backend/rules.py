from __future__ import annotations

import difflib
from typing import Iterable, List, Mapping

import pandas as pd

from backend.rule_registry import RULES
from utils import is_admin_role, require_columns
from schemas.user_schema import Finding, Issue


TERMINATED_USER = "terminated_user"
TERMINATED_ADMIN = "terminated_admin"
ADMIN_ROLE = "admin_role"


def find_best_column(columns, target, aliases, threshold=0.75):
    """
    Finds the best matching column name using fuzzy matching.
    """
    columns = list(columns)

    # Exact match
    if target in columns:
        return target, 1.0

    # Alias match
    for col in columns:
        if col in aliases:
            return col, 0.99

    # Fuzzy match
    best_match = None
    best_score = 0

    for col in columns:
        for alias in [target] + aliases:
            score = difflib.SequenceMatcher(None, col, alias).ratio()
            if score > best_score:
                best_score = score
                best_match = col

    if best_score >= threshold:
        return best_match, best_score

    return None, 0.0


def _normalize(series: Iterable[object]) -> pd.Series:
    return pd.Series(series).astype(str).str.strip().str.lower()


def normalize_columns(df):
    normalized = df.copy()

    # Normalize column names
    normalized.columns = [str(c).strip().lower() for c in normalized.columns]

    mappings = {
        "user_id": ["user_id", "uid", "id", "employee_id", "user"],
        "status": ["status", "account_status", "enabled", "active_flag", "acct_status"],
        "role": ["role", "access_role", "permission", "user_role", "group"],
        "termination_date": ["termination_date", "end_date", "terminated_at"],
    }

    rename_map = {}
    confidence_map: dict[str, float] = {}
    warning_threshold = 0.85

    for target, aliases in mappings.items():
        match, score = find_best_column(normalized.columns, target, aliases)
        if match and match != target:
            rename_map[match] = target
            confidence_map[target] = score
            if score < warning_threshold:
                print(
                    f"Warning: weak match for '{target}' -> '{match}' "
                    f"(confidence={score:.2f})"
                )

    # Apply renaming
    normalized = normalized.rename(columns=rename_map)

    # 🔍 DEBUG LOG (important)
    print("Column mapping applied:", rename_map)

    # Required column
    if "user_id" not in normalized.columns:
        raise ValueError("user_id column is required")

    # Graceful fallback for role
    if "role" not in normalized.columns:
        print("Warning: role column not found — defaulting to 'unknown'")
        normalized["role"] = "unknown"

    return normalized


def normalize_values(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    if "status" in normalized.columns:
        status_series = normalized["status"].astype(str).str.strip().str.lower()
        active_values = {"active", "enabled", "true", "1", "yes"}
        inactive_values = {"inactive", "disabled", "false", "0", "no"}
        normalized["status"] = status_series.map(
            lambda value: "active"
            if value in active_values
            else "inactive"
            if value in inactive_values
            else value
        )
    if "role" in normalized.columns:
        normalized["role"] = normalized["role"].astype(str).str.strip().str.lower()
    return normalized


def compute_flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # ---- VALUE NORMALIZATION (robust across messy inputs) ----
    def normalize_status(val):
        if pd.isna(val):
            return "inactive"
        v = str(val).strip().lower()

        if v in ["active", "enabled", "true", "1", "yes", "y"]:
            return "active"
        if v in ["inactive", "disabled", "false", "0", "no", "n"]:
            return "inactive"

        return v  # fallback (keeps unexpected values visible)

    df["status"] = df.get("status", pd.Series(dtype=str)).apply(normalize_status)

    # ---- ROLE NORMALIZATION ----
    df["role"] = df.get("role", pd.Series(dtype=str)).fillna("").astype(str).str.lower()

    # ---- TERMINATION DATE NORMALIZATION ----
    if "termination_date" in df.columns:
        df["termination_date"] = pd.to_datetime(df["termination_date"], errors="coerce")
    else:
        df["termination_date"] = pd.NaT

    # ---- FEATURE FLAGS (THIS IS THE KEY LAYER) ----
    df["is_active"] = df["status"] == "active"
    df["is_admin"] = df["role"].str.contains("admin", na=False)
    df["is_terminated"] = df["termination_date"].notna()

    # ---- DAYS SINCE TERMINATION ----
    today = pd.Timestamp.today().normalize()
    df["days_since_termination"] = (today - df["termination_date"]).dt.days.fillna(0)

    return df


def evaluate_rules(df):
    results = []

    for _, row in df.iterrows():
        for rule in RULES:
            if all(evaluate_condition(row, cond) for cond in rule["conditions"]):
                details = []
                for cond in rule["conditions"]:
                    field = cond["field"]
                    actual = row.get(field)

                    if field == "is_admin":
                        role_val = row.get("role", None)
                        if role_val is not None and role_val != "":
                            details.append(f"role='{role_val}' -> is_admin={actual}")
                        else:
                            details.append(f"is_admin={actual}")
                    elif field == "is_active":
                        status_val = row.get("status", None)
                        if status_val is not None and status_val != "":
                            details.append(f"status='{status_val}' -> is_active={actual}")
                        else:
                            details.append(f"is_active={actual}")
                    elif field == "is_terminated":
                        term_val = row.get("termination_date", None)
                        if term_val is not None and str(term_val) != "NaT":
                            details.append(
                                f"termination_date='{term_val}' -> is_terminated={actual}"
                            )
                        else:
                            details.append(f"is_terminated={actual}")
                    else:
                        details.append(f"{field}={actual}")

                explanation = (
                    f"Rule '{rule['id']}' triggered because " + ", ".join(details)
                )

                results.append(
                    {
                        "user_id": row["user_id"],
                        "issue_type": rule["id"],
                        "severity": rule["severity"],
                        "explanation": explanation,
                    }
                )

    return results


def evaluate_condition(row, cond):
    field = cond["field"]
    op = cond["operator"]
    value = cond["value"]

    if op == "==":
        return row[field] == value
    if op == "!=":
        return row[field] != value

    return False


def aggregate_results(results):
    priority = {"high": 3, "medium": 2, "low": 1}

    aggregated = {}

    for r in results:
        uid = r["user_id"]

        if uid not in aggregated:
            aggregated[uid] = {
                "user_id": uid,
                "severity": r["severity"],
                "issues": [],
            }

        aggregated[uid]["issues"].append(
            Issue(
                type=r["issue_type"],
                severity=r["severity"],
                explanation=r["explanation"],
            )
        )

        # upgrade severity if higher
        if priority[r["severity"]] > priority[aggregated[uid]["severity"]]:
            aggregated[uid]["severity"] = r["severity"]

    return [
        Finding(
            user_id=v["user_id"],
            severity=v["severity"],
            issues=v["issues"],
        )
        for v in aggregated.values()
    ]


def flag_users(
    user_access: pd.DataFrame, termination: pd.DataFrame
) -> List[Finding]:
    merged = merge_data(user_access, termination)
    evaluation_df = compute_flags(merged)
    results = evaluate_rules(evaluation_df)
    return aggregate_results(results)


def merge_data(user_access: pd.DataFrame, termination: pd.DataFrame) -> pd.DataFrame:
    user_access = normalize_columns(user_access)
    termination = normalize_columns(termination)
    user_access = normalize_values(user_access)
    termination = normalize_values(termination)
    require_columns(user_access, ["user_id"], "user_access.csv")
    require_columns(termination, ["user_id", "termination_date"], "termination.csv")

    return user_access.merge(
        termination[["user_id", "termination_date"]],
        on="user_id",
        how="left",
    )

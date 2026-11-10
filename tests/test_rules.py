import pandas as pd
import pytest

from backend.rules import (
    compute_flags,
    evaluate_condition,
    flag_users,
    merge_data,
    normalize_columns,
)
from tests.conftest import AS_OF, load


def _by_user(findings):
    return {f.user_id: f for f in findings}


def _types(finding):
    return {i.type for i in finding.issues}


def test_sample_files_produce_expected_findings():
    findings = _by_user(flag_users(load("sample_users.csv"), load("sample_termination.csv"), as_of=AS_OF))
    # test1: TRUE status, admin role, terminated 2024 -> terminated + privileged
    assert findings["test1"].severity == "critical"
    assert {"terminated_active", "terminated_privileged_active"} <= _types(findings["test1"])
    # u1003 is inactive: terminated but not active -> no termination finding
    assert "u1003" not in findings or "terminated_active" not in _types(findings["u1003"])


def test_messy_headers_are_mapped_and_recorded():
    log = []
    df = normalize_columns(load("test_users_messy.csv"), "user_access", log)
    assert {"user_id", "status", "role"} <= set(df.columns)
    by_target = {m.target: m for m in log}
    assert by_target["user_id"].matched_column == "employee_id"
    assert by_target["user_id"].method == "alias"
    assert by_target["status"].matched_column == "acct_status"
    assert by_target["termination_date"].method == "none"


def test_varied_status_values_normalize():
    df = compute_flags(merge_data(load("test_users_varied_status.csv"), load("test_termination_varied.csv")), AS_OF)
    status = dict(zip(df.user_id, df.status))
    assert status["u2001"] == "active"   # Enabled
    assert status["u2002"] == "active"   # TRUE
    assert status["u2003"] == "active"   # 1
    assert status["u2004"] == "inactive" # Disabled


@pytest.mark.parametrize(
    "role,tier,category",
    [
        ("Domain Admins", "critical", "domain_administration"),
        ("AccountAdmin", "critical", "cloud_administration"),
        ("db_owner", "high", "production_access"),
        ("sysadmin", "high", "production_access"),
        ("Billing Administrator", "medium", "sensitive_access"),
        ("system_admin", "low", "unclassified_admin"),
        ("viewer", "none", "standard_access"),
    ],
)
def test_privileged_catalog_is_wired_in(role, tier, category):
    df = compute_flags(pd.DataFrame({"user_id": ["x"], "status": ["active"], "role": [role]}), AS_OF)
    assert df.loc[0, "privilege_tier"] == tier
    assert df.loc[0, "privilege_category"] == category


def _single(role="viewer", status="active", term=None):
    users = pd.DataFrame({"user_id": ["u1"], "status": [status], "role": [role]})
    terms = pd.DataFrame({"user_id": ["u1"] if term else [], "termination_date": [term] if term else []})
    return flag_users(users, terms, as_of=AS_OF)


def test_catalog_tiers_map_to_finding_severity():
    assert _single("Global Administrator")[0].severity == "high"
    assert _single("db_owner")[0].severity == "medium"
    assert _single("Power Users")[0].severity == "low"
    assert _single("viewer") == []


def test_terminated_privileged_is_critical():
    f = _single("Domain Admins", term="2026-10-01")[0]
    assert f.severity == "critical"
    assert {"terminated_privileged_active", "terminated_active", "privileged_access_critical"} <= _types(f)


def test_grace_period_downgrades_recent_termination():
    # 2026-11-10 is a Tuesday; terminated Monday -> 1 business day -> within default grace of 1
    f = _single(term="2026-11-09")[0]
    assert _types(f) == {"terminated_active_within_grace"}
    assert f.severity == "medium"
    # Terminated the previous Thursday -> 3 business days -> past grace
    f = _single(term="2026-11-05")[0]
    assert _types(f) == {"terminated_active"}
    assert f.severity == "high"


def test_grace_period_is_configurable(monkeypatch):
    monkeypatch.setenv("TERMINATION_GRACE_BUSINESS_DAYS", "5")
    assert _types(_single(term="2026-11-05")[0]) == {"terminated_active_within_grace"}


def test_future_termination_is_not_terminated_yet():
    assert _single(term="2026-12-31") == []


def test_inactive_terminated_user_is_clean():
    assert _single(status="disabled", term="2025-01-01") == []


def test_findings_are_reproducible_with_inputs():
    issue = _single("Domain Admins", term="2026-10-01")[0].issues[0]
    assert issue.rule_version >= 1
    assert issue.inputs["user_id"] == "u1"
    assert issue.inputs["termination_date"] == "2026-10-01"
    assert issue.row_ref == 0
    assert issue.control_refs


@pytest.mark.parametrize(
    "op,value,actual,expected",
    [
        ("==", 1, 1, True),
        ("!=", 1, 2, True),
        (">", 5, 6, True),
        ("<", 5, 6, False),
        (">=", 5, 5, True),
        ("<=", 5, 4, True),
        ("in", ["a", "b"], "a", True),
        ("not_in", ["a", "b"], "c", True),
        ("contains", "ADMIN", "sys_admin", True),
        ("regex", r"^prod[-_ ]", "prod_admin", True),
        ("regex", r"^prod[-_ ]", "nonprod", False),
        ("days_since", {"op": ">", "days": 30}, "2026-09-01", True),
        ("days_since", {"op": ">", "days": 30}, "2026-11-01", False),
        ("days_since", {"op": ">", "days": 30}, None, False),
        (">", 5, None, False),
    ],
)
def test_operators(op, value, actual, expected):
    row = {"f": actual}
    assert evaluate_condition(row, {"field": "f", "operator": op, "value": value}, AS_OF) is expected


def test_unknown_operator_raises():
    with pytest.raises(ValueError):
        evaluate_condition({"f": 1}, {"field": "f", "operator": "~=", "value": 1})


def test_ids_match_across_types():
    users = pd.DataFrame({"user_id": [101], "status": ["active"], "role": ["viewer"]})
    terms = pd.DataFrame({"user_id": [" 101 "], "termination_date": ["2025-01-01"]})
    assert _types(flag_users(users, terms, as_of=AS_OF)[0]) == {"terminated_active"}

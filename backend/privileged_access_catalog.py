from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PrivilegedAccessMatch:
    is_privileged: bool
    privilege_category: str
    risk_tier: str
    matched_pattern: str


PRIVILEGED_ACCESS_CATALOG: dict[str, dict[str, Iterable[str]]] = {
    "critical": {
        "domain_administration": {
            "domain admins",
            "enterprise admins",
            "schema admins",
            "organization management",
        },
        "identity_administration": {
            "global administrator",
            "privileged role administrator",
            "security administrator",
            "conditional access administrator",
        },
        "cloud_administration": {
            "accountadmin",
            "securityadmin",
            "administratoraccess",
            "iamfullaccess",
        },
    },
    "high": {
        "directory_operations": {
            "account operators",
            "server operators",
            "backup operators",
            "dnsadmins",
            "group policy creator owners",
            "user administrator",
            "exchange administrator",
            "sharepoint administrator",
            "application administrator",
        },
        "production_access": {
            "production admin",
            "prod admin",
            "database owner",
            "db_owner",
            "sysadmin",
        },
    },
    "medium": {
        "sensitive_access": {
            "power users",
            "helpdesk administrator",
            "billing administrator",
            "compliance administrator",
            "report reader",
        },
    },
}


def _clean(value: object) -> str:
    return str(value or "").strip().lower()


def classify_privileged_access(access_value: object) -> PrivilegedAccessMatch:
    """Classify a role/group/access string against the privileged access catalog."""
    access_text = _clean(access_value)
    if not access_text:
        return PrivilegedAccessMatch(False, "standard_access", "low", "")

    for risk_tier, categories in PRIVILEGED_ACCESS_CATALOG.items():
        for category, patterns in categories.items():
            for pattern in patterns:
                pattern_text = _clean(pattern)
                if pattern_text and pattern_text in access_text:
                    return PrivilegedAccessMatch(True, category, risk_tier, pattern_text)

    return PrivilegedAccessMatch(False, "standard_access", "low", "")

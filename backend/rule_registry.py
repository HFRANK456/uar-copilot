# backend/rule_registry.py
"""Declarative, versioned rule set.

Each rule is pure data: every condition must hold for the rule to fire.
Supported operators (see rules.evaluate_condition):
    ==, !=, >, >=, <, <=, in, not_in, contains, regex, days_since

`days_since` compares the age in days of a date field:
    {"field": "last_login_at", "operator": "days_since", "value": {"op": ">", "days": 90}}

Bump a rule's `version` whenever its conditions or severity change, so stored
findings can always be re-performed against the exact rule that produced them.

Severity scale: critical > high > medium > low.
Privileged access by catalog tier maps one step down (catalog critical -> high
finding, high -> medium, medium -> low): holding privileged access needs review,
it is not by itself a violation. `critical` is reserved for terminated users who
still hold active privileged access.
"""

RULE_SET_VERSION = "2026.11.2"

# Accounts with no login in this many days count as dormant.
DORMANT_DAYS = 90

RULES = [
    {
        "id": "terminated_privileged_active",
        "version": 1,
        "description": "Terminated user still active and holding privileged access",
        "conditions": [
            {"field": "is_terminated", "operator": "==", "value": True},
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "is_privileged", "operator": "==", "value": True},
        ],
        "severity": "critical",
        "control_refs": ["CC6.1", "CC6.2", "CC6.3"],
    },
    {
        "id": "terminated_active",
        "version": 2,
        "description": "User is terminated but still active past the grace period",
        "conditions": [
            {"field": "is_terminated", "operator": "==", "value": True},
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "within_termination_grace", "operator": "==", "value": False},
        ],
        "severity": "high",
        "control_refs": ["CC6.2", "CC6.3"],
    },
    {
        "id": "terminated_active_within_grace",
        "version": 1,
        "description": "User is terminated and still active, but within the grace period",
        "conditions": [
            {"field": "is_terminated", "operator": "==", "value": True},
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "within_termination_grace", "operator": "==", "value": True},
        ],
        "severity": "medium",
        "control_refs": ["CC6.2"],
    },
    {
        "id": "privileged_access_critical",
        "version": 1,
        "description": "Active account holds critical-tier privileged access",
        "conditions": [
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "privilege_tier", "operator": "==", "value": "critical"},
        ],
        "severity": "high",
        "control_refs": ["CC6.1", "CC6.3"],
    },
    {
        "id": "privileged_access_high",
        "version": 1,
        "description": "Active account holds high-tier privileged access",
        "conditions": [
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "privilege_tier", "operator": "==", "value": "high"},
        ],
        "severity": "medium",
        "control_refs": ["CC6.1", "CC6.3"],
    },
    {
        "id": "privileged_access_medium",
        "version": 1,
        "description": "Active account holds medium-tier sensitive access",
        "conditions": [
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "privilege_tier", "operator": "==", "value": "medium"},
        ],
        "severity": "low",
        "control_refs": ["CC6.3"],
    },
    {
        "id": "external_account_active",
        "version": 1,
        "description": "Active account belongs to someone outside the company (not on the HR roster)",
        "conditions": [
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "account_type", "operator": "==", "value": "external"},
        ],
        "severity": "medium",
        "control_refs": ["CC6.2", "CC6.3"],
        "scope": "account",
    },
    {
        "id": "dormant_account",
        "version": 1,
        "description": f"Active account with no login in more than {DORMANT_DAYS} days",
        "conditions": [
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "last_login_at", "operator": "days_since", "value": {"op": ">", "days": DORMANT_DAYS}},
        ],
        "severity": "low",
        "control_refs": ["CC6.2"],
        "scope": "account",
    },
    {
        "id": "admin_role",
        "version": 2,
        "description": "Role looks administrative but is not in the privileged access catalog",
        "conditions": [
            {"field": "is_active", "operator": "==", "value": True},
            {"field": "privilege_category", "operator": "==", "value": "unclassified_admin"},
        ],
        "severity": "low",
        "control_refs": ["CC6.3"],
    },
]

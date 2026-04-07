# backend/rule_registry.py

RULES = [
    {
        "id": "terminated_active",
        "description": "User is terminated but still active",
        "conditions": [
            {"field": "is_terminated", "operator": "==", "value": True},
            {"field": "is_active", "operator": "==", "value": True},
        ],
        "severity": "high",
    },
    {
        "id": "admin_role",
        "description": "User has admin privileges",
        "conditions": [
            {"field": "is_admin", "operator": "==", "value": True},
        ],
        "severity": "low",
    },
]

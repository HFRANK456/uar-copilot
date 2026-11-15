"""Jira Cloud connector (REST API v3).

Setup on the customer's side: an Atlassian account that is a Jira admin creates an API token
(id.atlassian.com > Security > API tokens) and gives us the site (company.atlassian.net),
that account's email, and the token. Optionally a project key for remediation tickets.

What it pulls: every Atlassian user account and app (bot) account, and every group they are
in. In Jira, access is granted through groups, so each group is an entitlement
("jira-administrators", "jira-software-users", ...). Portal-only customer accounts are skipped.

Limits worth knowing (Atlassian's, not ours): user emails are often hidden by privacy settings,
so matching to HR may fall back to display names (queued for a person to confirm), and Jira's
API has no last-login date.

Writes: only `create_ticket`, and only when a project key is configured.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from backend.connectors.base import MAX_PAGES, AccessPull, ConnectorError, check, client

PAGE = 50


def normalize_site(site: str) -> str:
    site = (site or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    if site and "." not in site:
        site += ".atlassian.net"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.atlassian\.net", site):
        raise ConnectorError("Enter your Jira Cloud site, like yourcompany.atlassian.net.")
    return f"https://{site}"


def validate(config: Dict[str, Any], secret: Dict[str, Any]) -> Dict[str, Any]:
    email = (config.get("email") or "").strip().lower()
    if "@" not in email:
        raise ConnectorError("Enter the email of the Atlassian account the API token belongs to.")
    if not (secret.get("api_token") or "").strip():
        raise ConnectorError("Paste the Atlassian API token.")
    project = (config.get("project_key") or "").strip().upper() or None
    if project and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", project):
        raise ConnectorError("Project keys look like IT or SEC (letters and numbers).")
    return {"site": normalize_site(config.get("site", "")), "email": email, "project_key": project,
            "issue_type": (config.get("issue_type") or "Task").strip() or "Task"}


def _client(config: Dict[str, Any], secret: Dict[str, Any]):
    return client(base_url=config["site"], auth=(config["email"], secret["api_token"].strip()),
                  headers={"Accept": "application/json"})


def test(config: Dict[str, Any], secret: Dict[str, Any]) -> str:
    with _client(config, secret) as c:
        me = check(c.get("/rest/api/3/myself"), "Check the account")
    return f"Signed in to {config['site']} as {me.get('displayName') or config['email']}."


def pull(config: Dict[str, Any], secret: Dict[str, Any]) -> AccessPull:
    raw: List[Any] = []
    users: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    members: Dict[str, List[str]] = {}
    with _client(config, secret) as c:
        start = 0
        for _ in range(MAX_PAGES):
            page = check(c.get("/rest/api/3/users/search", params={"startAt": start, "maxResults": 1000}), "List users")
            raw.append({"request": "users/search", "startAt": start, "response": page})
            users.extend(page)
            if len(page) < 1000:
                break
            start += len(page)
        start = 0
        for _ in range(MAX_PAGES):
            page = check(c.get("/rest/api/3/group/bulk", params={"startAt": start, "maxResults": PAGE}), "List groups")
            raw.append({"request": "group/bulk", "startAt": start, "response": page})
            groups.extend(page.get("values", []))
            if page.get("isLast", True):
                break
            start += PAGE
        for g in groups:
            start = 0
            for _ in range(MAX_PAGES):
                page = check(c.get("/rest/api/3/group/member", params={
                    "groupId": g["groupId"], "startAt": start, "maxResults": PAGE, "includeInactiveUsers": "true"}),
                    f"List members of {g.get('name')}")
                raw.append({"request": "group/member", "group": g.get("name"), "startAt": start, "response": page})
                for m in page.get("values", []):
                    members.setdefault(m["accountId"], []).append(g["name"])
                if page.get("isLast", True):
                    break
                start += PAGE

    rows: List[Dict[str, Any]] = []
    hidden = 0
    for u in users:
        kind = u.get("accountType")
        if kind not in ("atlassian", "app"):
            continue  # "customer" = service-desk portal users, not workforce access
        email = (u.get("emailAddress") or "").lower() or None
        if kind == "atlassian" and not email:
            hidden += 1
        name = u.get("displayName") or u["accountId"]
        base = {"native_id": u["accountId"],
                # App accounts are bots; the "svc-" prefix makes the ingest classify them as service accounts.
                "username": f"svc-{name}" if kind == "app" else (email or name),
                "email": email, "display_name": name,
                "status": "active" if u.get("active", True) else "inactive", "last_login": None}
        for group in sorted(set(members.get(u["accountId"], []))) or [""]:
            rows.append({**base, "role": group})
    warnings = ["Jira's API has no last-login date, so the dormant-account rule can't run for Jira."]
    if hidden:
        warnings.append(f"{hidden} user(s) hide their email in their Atlassian profile; they'll be matched to "
                        "people by name, which you confirm in the identity queue.")
    return AccessPull("Jira", rows, raw, warnings)


def create_ticket(config: Dict[str, Any], secret: Dict[str, Any], summary: str, description: str,
                  labels: List[str] | None = None) -> str:
    """Create one issue and return its key (e.g. IT-123). Priority isn't set because many
    projects leave it off the create screen (Jira then rejects the whole request); the risk
    level goes in the labels and description instead."""
    if not config.get("project_key"):
        raise ConnectorError("Add a Jira project key to this connection to create tickets.")
    doc = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in description.splitlines() if line
    ]}
    fields: Dict[str, Any] = {"project": {"key": config["project_key"]}, "summary": summary[:250],
                              "description": doc, "issuetype": {"name": config.get("issue_type") or "Task"},
                              "labels": ["access-review", *(labels or [])]}
    with _client(config, secret) as c:
        resp = c.post("/rest/api/3/issue", json={"fields": fields})
        if resp.status_code == 400:
            try:
                errs = resp.json().get("errors") or resp.json().get("errorMessages")
            except ValueError:
                errs = None
            raise ConnectorError(f"Jira didn't accept the ticket: {errs or 'check the project key and issue type'}")
        body = check(resp, "Create ticket")
    return body["key"]

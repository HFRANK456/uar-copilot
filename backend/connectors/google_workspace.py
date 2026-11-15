"""Google Workspace connector (Admin SDK Directory API, read-only).

Setup on the customer's side (once):
  1. In Google Cloud, create a service account and a JSON key for it, and enable the Admin SDK API.
  2. In the Google Admin console (Security > API controls > Domain-wide delegation), allow the
     service account's client ID these read-only scopes: SCOPES below.
  3. Give us the JSON key and the email of a Workspace admin to act as (read-only calls only).

What it pulls: every user (email, name, suspended/archived, last sign-in) and every admin
role assignment. Each user becomes an account; each admin role an entitlement ("Super Admin",
"Groups Admin", ...). Users without an admin role are reviewed as plain "Account access".
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from jose import jwt

from backend.connectors.base import MAX_PAGES, AccessPull, ConnectorError, check, client

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.directory.rolemanagement.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://admin.googleapis.com/admin/directory/v1"
NEVER = "1970-01-01T00:00:00.000Z"


def validate(config: Dict[str, Any], secret: Dict[str, Any]) -> Dict[str, Any]:
    key = secret.get("service_account") or {}
    missing = [k for k in ("client_email", "private_key") if not key.get(k)]
    if missing:
        raise ConnectorError("That doesn't look like a service account key file (missing "
                             + ", ".join(missing) + "). Download a JSON key for the service account.")
    admin = (config.get("admin_email") or "").strip().lower()
    if "@" not in admin:
        raise ConnectorError("Enter the email of a Google Workspace admin for the connector to act as.")
    return {"admin_email": admin, "service_account_email": key["client_email"],
            "domain": admin.split("@", 1)[1]}


def _token(config: Dict[str, Any], secret: Dict[str, Any]) -> str:
    key = secret["service_account"]
    now = int(time.time())
    claims = {"iss": key["client_email"], "sub": config["admin_email"], "scope": " ".join(SCOPES),
              "aud": key.get("token_uri") or TOKEN_URI, "iat": now, "exp": now + 3600}
    try:
        assertion = jwt.encode(claims, key["private_key"], algorithm="RS256")
    except Exception as exc:  # malformed key
        raise ConnectorError("The service account's private key couldn't be used to sign in.") from exc
    with client() as c:
        resp = c.post(key.get("token_uri") or TOKEN_URI, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error_description") or resp.json().get("error") or ""
        except ValueError:
            pass
        hint = (" Check that domain-wide delegation allows this service account's client ID with the "
                "read-only scopes, and that the admin email is a Workspace admin.")
        raise ConnectorError(f"Google refused the sign-in ({detail or resp.status_code}).{hint}")
    return resp.json()["access_token"]


def _paged(c, url: str, params: Dict[str, Any], what: str, raw: List[Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        q = dict(params, **({"pageToken": token} if token else {}))
        body = check(c.get(url, params=q), what)
        raw.append({"request": what, "params": q, "response": body})
        items.extend(body.get("items") or body.get("users") or [])
        token = body.get("nextPageToken")
        if not token:
            return items
    raise ConnectorError(f"{what}: too many pages; stopped to be safe.")


def role_label(name: str, is_super: bool = False) -> str:
    """'_GROUPS_ADMIN_ROLE' -> 'Groups Admin'; Google's built-in super admin role -> 'Super Admin'."""
    if is_super:
        return "Super Admin"
    if name.startswith("_") and name.endswith("_ROLE"):
        return name.strip("_")[: -len("_ROLE")].replace("_", " ").title().replace("Seed Admin", "Super Admin")
    return name


def test(config: Dict[str, Any], secret: Dict[str, Any]) -> str:
    token = _token(config, secret)
    with client(headers={"Authorization": f"Bearer {token}"}) as c:
        check(c.get(f"{API}/users", params={"customer": "my_customer", "maxResults": 1}), "List users")
    return f"Signed in as {config['admin_email']} and read the user directory."


def pull(config: Dict[str, Any], secret: Dict[str, Any]) -> AccessPull:
    token = _token(config, secret)
    raw: List[Any] = []
    with client(headers={"Authorization": f"Bearer {token}"}) as c:
        users = _paged(c, f"{API}/users", {"customer": "my_customer", "maxResults": 500, "projection": "basic",
                                            "orderBy": "email"}, "List users", raw)
        roles = _paged(c, f"{API}/customer/my_customer/roles", {"maxResults": 100}, "List admin roles", raw)
        assignments = _paged(c, f"{API}/customer/my_customer/roleassignments", {"maxResults": 200},
                             "List admin role assignments", raw)

    role_names = {str(r["roleId"]): role_label(r.get("roleName", ""), bool(r.get("isSuperAdminRole"))) for r in roles}
    by_user: Dict[str, List[str]] = {}
    for a in assignments:
        if a.get("assigneeType", "user") != "user":
            continue  # group-assigned roles: listed for the person via the group in a later version
        by_user.setdefault(str(a.get("assignedTo")), []).append(role_names.get(str(a.get("roleId")), str(a.get("roleId"))))

    rows: List[Dict[str, Any]] = []
    for u in users:
        email = (u.get("primaryEmail") or "").lower()
        if not email:
            continue
        status = "inactive" if (u.get("suspended") or u.get("archived")) else "active"
        last = u.get("lastLoginTime")
        base = {"native_id": email, "username": email, "email": email,
                "display_name": (u.get("name") or {}).get("fullName"), "status": status,
                "last_login": None if not last or last == NEVER else last}
        roles_for = sorted(set(by_user.get(str(u.get("id")), [])))
        if u.get("isAdmin") and "Super Admin" not in roles_for:
            roles_for.append("Super Admin")  # the isAdmin flag is the source of truth for super admin
        for role in roles_for or [""]:
            rows.append({**base, "role": role})
    warnings = []
    if any(a.get("assigneeType") == "group" for a in assignments):
        warnings.append("Some admin roles are assigned to groups; those aren't expanded to people yet.")
    return AccessPull("Google Workspace", rows, raw, warnings)

"""Microsoft Entra ID connector (Microsoft Graph v1.0, read-only, app-only).

Setup on the customer's side (once, by an admin of their Microsoft 365 / Entra tenant):
  1. Entra admin center > App registrations > New registration ("UAR Copilot", single tenant).
  2. API permissions > Add > Microsoft Graph > Application permissions: PERMISSIONS below,
     then "Grant admin consent".
  3. Certificates & secrets > New client secret. Give us the tenant ID, the application
     (client) ID and the secret's Value.

What it pulls: every user (enabled/disabled, member or guest, last sign-in when the tenant
has Entra ID P1 or P2) and every directory role assignment ("Global Administrator",
"User Administrator", ...) as entitlements. Users without a role are reviewed as plain
"Account access". Nothing is written to the tenant.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from backend.connectors.base import MAX_PAGES, AccessPull, ConnectorError, check, client

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
PERMISSIONS = ["User.Read.All", "RoleManagement.Read.Directory", "AuditLog.Read.All"]
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+")
USER_FIELDS = "id,displayName,userPrincipalName,mail,accountEnabled,userType"
# Graph caps pages at 120 users when signInActivity is selected.
PAGE = 120

class AccessDenied(ConnectorError):
    pass


# Token errors worth translating (codes from Microsoft's AADSTS reference).
TOKEN_HINTS = {
    "AADSTS700016": "the application (client) ID wasn't found in that tenant",
    "AADSTS7000215": "the client secret is wrong (paste the secret's Value, not its ID)",
    "AADSTS7000222": "the client secret has expired; create a new one",
    "AADSTS90002": "the tenant wasn't found; check the tenant ID",
    "AADSTS900023": "the tenant ID isn't valid",
}


def validate(config: Dict[str, Any], secret: Dict[str, Any]) -> Dict[str, Any]:
    tenant = (config.get("tenant_id") or "").strip().lower()
    if not (GUID.fullmatch(tenant) or DOMAIN.fullmatch(tenant)):
        raise ConnectorError("Enter the Directory (tenant) ID from the app's Overview page, or the tenant's "
                             "domain like contoso.onmicrosoft.com.")
    client_id = (config.get("client_id") or "").strip().lower()
    if not GUID.fullmatch(client_id):
        raise ConnectorError("Enter the Application (client) ID from the app's Overview page.")
    if not (secret.get("client_secret") or "").strip():
        raise ConnectorError("Paste the client secret's Value.")
    return {"tenant_id": tenant, "client_id": client_id}


def _token(config: Dict[str, Any], secret: Dict[str, Any]) -> str:
    with client() as c:
        resp = c.post(f"{LOGIN}/{config['tenant_id']}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": config["client_id"],
            "client_secret": secret["client_secret"].strip(), "scope": "https://graph.microsoft.com/.default"})
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        desc = body.get("error_description") or body.get("error") or str(resp.status_code)
        hint = next((h for code, h in TOKEN_HINTS.items() if code in desc), None)
        raise ConnectorError(f"Microsoft refused the sign-in: {hint or desc.splitlines()[0]}.")
    return resp.json()["access_token"]


def _pages(c, url: str, params: Optional[Dict[str, Any]], what: str, raw: List[Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        resp = c.get(url, params=params)
        if resp.status_code == 403:
            raise AccessDenied(f"{what}: access denied. In the app registration, check the API permissions "
                                 f"({', '.join(PERMISSIONS)}) and that admin consent was granted.")
        body = check(resp, what)
        raw.append({"request": what, "url": str(resp.request.url), "response": body})
        items.extend(body.get("value", []))
        url, params = body.get("@odata.nextLink"), None  # the next link carries its own query
        if not url:
            return items
    raise ConnectorError(f"{what}: too many pages; stopped to be safe.")


def test(config: Dict[str, Any], secret: Dict[str, Any]) -> str:
    token = _token(config, secret)
    with client(headers={"Authorization": f"Bearer {token}"}) as c:
        check(c.get(f"{GRAPH}/users", params={"$top": 1, "$select": "id"}), "List users")
    return f"Signed in to tenant {config['tenant_id']} and read the user directory."


def pull(config: Dict[str, Any], secret: Dict[str, Any]) -> AccessPull:
    token = _token(config, secret)
    raw: List[Any] = []
    warnings: List[str] = []
    with client(headers={"Authorization": f"Bearer {token}"}) as c:
        try:
            users = _pages(c, f"{GRAPH}/users", {"$select": USER_FIELDS + ",signInActivity", "$top": PAGE},
                           "List users", raw)
        except AccessDenied:
            # Last sign-in needs AuditLog.Read.All and an Entra ID P1/P2 licence; carry on without it.
            raw.clear()
            users = _pages(c, f"{GRAPH}/users", {"$select": USER_FIELDS, "$top": PAGE}, "List users", raw)
            warnings.append("Last sign-in dates aren't available (they need Entra ID P1 or P2 and the "
                            "AuditLog.Read.All permission), so the dormant-account rule can't run for Entra ID.")
        roles = _pages(c, f"{GRAPH}/directoryRoles", {"$expand": "members"}, "List directory roles", raw)

    by_user: Dict[str, List[str]] = {}
    via_group = 0
    for role in roles:
        for m in role.get("members", []):
            kind = m.get("@odata.type", "")
            if kind == "#microsoft.graph.user":
                by_user.setdefault(m["id"], []).append(role.get("displayName") or role.get("id"))
            elif kind == "#microsoft.graph.group":
                via_group += 1

    rows: List[Dict[str, Any]] = []
    guests = 0
    for u in users:
        upn = (u.get("userPrincipalName") or "").lower()
        if not upn:
            continue
        guest = (u.get("userType") or "").lower() == "guest"
        guests += guest
        sign_in = u.get("signInActivity") or {}
        base = {"native_id": upn, "username": upn,
                # A guest's UPN is mangled (name_domain#EXT#@tenant); their real address is `mail`.
                "email": (u.get("mail") or ("" if guest else upn)).lower() or None,
                "display_name": u.get("displayName"),
                "status": "active" if u.get("accountEnabled", True) else "inactive",
                "last_login": sign_in.get("lastSuccessfulSignInDateTime") or sign_in.get("lastSignInDateTime")}
        for role in sorted(set(by_user.get(u["id"], []))) or [""]:
            rows.append({**base, "role": role})
    if via_group:
        warnings.append(f"{via_group} admin role assignment(s) go to groups; those aren't expanded to people yet.")
    if guests:
        warnings.append(f"{guests} guest account(s) from outside the company; they'll show as external unless "
                        "they're on the HR roster.")
    return AccessPull("Microsoft Entra ID", rows, raw, warnings)

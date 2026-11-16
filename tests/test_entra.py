"""Microsoft Entra ID connector against recorded-format Microsoft Graph responses (no network)."""
import json
from urllib.parse import parse_qs

import httpx
import pytest

from backend.connectors import entra_id
from backend.connectors.base import ConnectorError
from tests.test_campaigns_db import api  # noqa: F401  (fixture)
from tests.test_connectors import _add, _campaign_with_hr, mock_http, secret_key  # noqa: F401  (fixtures)

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
APP = "11111111-2222-3333-4444-555555555555"
CONFIG = {"tenant_id": TENANT.upper(), "client_id": APP}
SECRET = {"client_secret": "Abc8Q~not-a-real-secret"}

USERS_P1 = {"@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=page2", "value": [
    {"id": "u-dana", "displayName": "Dana Reyes", "userPrincipalName": "dana.reyes@acme.io", "mail": "dana.reyes@acme.io",
     "accountEnabled": True, "userType": "Member",
     "signInActivity": {"lastSignInDateTime": "2026-11-14T08:00:00Z", "lastSuccessfulSignInDateTime": "2026-11-14T08:00:00Z"}},
    {"id": "u-jordan", "displayName": "Jordan Kim", "userPrincipalName": "jordan.kim@acme.io", "mail": "jordan.kim@acme.io",
     "accountEnabled": True, "userType": "Member", "signInActivity": {"lastSignInDateTime": "2026-10-01T10:00:00Z"}},
]}
USERS_P2 = {"value": [
    {"id": "u-alex", "displayName": "Alex Chen", "userPrincipalName": "alex.chen@acme.io", "mail": None,
     "accountEnabled": False, "userType": "Member", "signInActivity": None},
    {"id": "u-guest", "displayName": "J Doe (Vendor)", "userPrincipalName": "j.doe_vendor.example#EXT#@acme.onmicrosoft.com",
     "mail": "j.doe@vendor.example", "accountEnabled": True, "userType": "Guest"},
]}
ROLES = {"value": [
    {"id": "r1", "displayName": "Global Administrator", "members": [
        {"@odata.type": "#microsoft.graph.user", "id": "u-dana"},
        {"@odata.type": "#microsoft.graph.user", "id": "u-jordan"}]},
    {"id": "r2", "displayName": "Helpdesk Administrator", "members": [
        {"@odata.type": "#microsoft.graph.group", "id": "g-helpdesk"},
        {"@odata.type": "#microsoft.graph.servicePrincipal", "id": "sp-1"}]},
    {"id": "r3", "displayName": "User Administrator", "members": [
        {"@odata.type": "#microsoft.graph.user", "id": "u-alex"}]},
]}


def graph(request, licensed=True):
    url = str(request.url)
    if url.startswith("https://login.microsoftonline.com/"):
        form = parse_qs(request.content.decode())
        assert TENANT in url and form["client_id"] == [APP] and form["grant_type"] == ["client_credentials"]
        assert form["scope"] == ["https://graph.microsoft.com/.default"]
        if form["client_secret"] != [SECRET["client_secret"]]:
            return httpx.Response(401, json={"error": "invalid_client", "error_description":
                                             "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: x"})
        return httpx.Response(200, json={"access_token": "eyJ.test", "token_type": "Bearer", "expires_in": 3599})
    assert request.headers["Authorization"] == "Bearer eyJ.test"
    if request.url.path == "/v1.0/users":
        if "skiptoken" in url:
            return httpx.Response(200, json=USERS_P2)
        if "signInActivity" in url and not licensed:
            return httpx.Response(403, json={"error": {"code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant"}})
        if "signInActivity" in url:
            assert "%24top=120" in url or "$top=120" in url
        return httpx.Response(200, json=USERS_P1)
    if request.url.path == "/v1.0/directoryRoles":
        return httpx.Response(200, json=ROLES)
    return httpx.Response(404, json={"error": {"code": "NotFound"}})


def test_validate():
    assert entra_id.validate(CONFIG, SECRET) == {"tenant_id": TENANT, "client_id": APP}
    assert entra_id.validate({**CONFIG, "tenant_id": "Contoso.onmicrosoft.com"}, SECRET)["tenant_id"] == "contoso.onmicrosoft.com"
    with pytest.raises(ConnectorError, match="tenant"):
        entra_id.validate({**CONFIG, "tenant_id": "not a tenant"}, SECRET)
    with pytest.raises(ConnectorError, match="client"):
        entra_id.validate({**CONFIG, "client_id": "abc"}, SECRET)
    with pytest.raises(ConnectorError, match="secret"):
        entra_id.validate(CONFIG, {})


def test_pull(mock_http):
    mock_http.handler = graph
    cfg = entra_id.validate(CONFIG, SECRET)
    pulled = entra_id.pull(cfg, SECRET)
    rows = {(r["username"], r["role"]): r for r in pulled.rows}
    assert set(rows) == {
        ("dana.reyes@acme.io", "Global Administrator"), ("jordan.kim@acme.io", "Global Administrator"),
        ("alex.chen@acme.io", "User Administrator"),
        ("j.doe_vendor.example#ext#@acme.onmicrosoft.com", ""),
    }
    assert rows[("dana.reyes@acme.io", "Global Administrator")]["last_login"] == "2026-11-14T08:00:00Z"
    alex = rows[("alex.chen@acme.io", "User Administrator")]
    assert alex["status"] == "inactive" and alex["email"] == "alex.chen@acme.io"  # falls back to the UPN
    guest = rows[("j.doe_vendor.example#ext#@acme.onmicrosoft.com", "")]
    assert guest["email"] == "j.doe@vendor.example"
    assert any("go to groups" in w for w in pulled.warnings) and any("guest" in w for w in pulled.warnings)
    assert not any("Last sign-in" in w for w in pulled.warnings)
    assert [p["request"] for p in json.loads(pulled.raw_bytes())["pages"]] == ["List users", "List users",
                                                                              "List directory roles"]


def test_pull_without_premium_licence_carries_on(mock_http):
    mock_http.handler = lambda r: graph(r, licensed=False)
    pulled = entra_id.pull(entra_id.validate(CONFIG, SECRET), SECRET)
    assert len({r["username"] for r in pulled.rows}) == 4
    assert any("Last sign-in dates aren't available" in w for w in pulled.warnings)


def test_sign_in_errors_are_explained(mock_http):
    mock_http.handler = graph
    cfg = entra_id.validate(CONFIG, SECRET)
    with pytest.raises(ConnectorError, match="client secret is wrong"):
        entra_id.test(cfg, {"client_secret": "wrong"})
    mock_http.handler = lambda r: (graph(r) if "login.microsoftonline" in str(r.url)
                                   else httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}}))
    with pytest.raises(ConnectorError, match="admin consent"):
        entra_id.pull(cfg, SECRET)


def test_entra_through_the_api(api, secret_key, mock_http):
    r = _add(api, "entra_id", CONFIG, SECRET)
    assert r.status_code == 201, r.text
    conn = r.json()
    assert conn["label"] == "Microsoft Entra ID" and conn["config"] == {"tenant_id": TENANT, "client_id": APP}
    assert SECRET["client_secret"] not in r.text

    mock_http.handler = graph
    assert api.post(f"/connections/{conn['id']}/test").json()["status"] == "ok"
    cid = _campaign_with_hr(api)
    r = api.post(f"/campaigns/{cid}/sources/connection", json={"connection_id": conn["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["snapshot"]["application"] == "Microsoft Entra ID" and r.json()["counts"]["accounts"] == 4

    for item in api.post(f"/campaigns/{cid}/identities/resolve").json()["items"]:
        if item["status"] in ("ambiguous", "unmatched"):
            api.post(f"/campaigns/{cid}/identities/{item['account_id']}", json={"account_type": "external"})
    assert api.post(f"/campaigns/{cid}/analyze").status_code == 200
    found = {(i["native_id"], i["entitlement"]): i["severity"]
             for i in api.get(f"/campaigns/{cid}/review-items").json()["items"]}
    assert found[("jordan.kim@acme.io", "Global Administrator")] == "critical"  # terminated, still Global Admin
    assert found[("dana.reyes@acme.io", "Global Administrator")] == "high"

"""Connectors: Google Workspace and Jira Cloud against recorded-format API responses.

No network: every request goes through httpx.MockTransport (via base.transport_factory), and the
mock answers in the shapes Google's Admin SDK and Jira's REST v3 document.
"""
import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from backend.connectors import base, google_workspace, jira_cloud
from backend.connectors.base import ConnectorError
from backend.privileged_access_catalog import classify_privileged_access
from tests.conftest import DATA
from tests.test_campaigns_db import OVR, _items, _signed_off, api  # noqa: F401  (api is a fixture)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
SERVICE_ACCOUNT = {"type": "service_account", "client_email": "uar@acme-uar.iam.gserviceaccount.com",
                   "private_key": PRIVATE_PEM, "token_uri": "https://oauth2.googleapis.com/token"}
JIRA_TOKEN = "ATATT3xFfGF0-test-token-not-real"


@pytest.fixture
def mock_http(monkeypatch):
    """Route every connector request to `handler`; tests set .handler and read .calls."""
    class Mock:
        calls = []
        handler = None

    def transport():
        def dispatch(request):
            Mock.calls.append(request)
            return Mock.handler(request)
        return httpx.MockTransport(dispatch)

    monkeypatch.setattr(base, "transport_factory", transport)
    Mock.calls = []
    return Mock


def _q(request):
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


# ---------------------------------------------------------------------------
# Google Workspace
# ---------------------------------------------------------------------------

GOOGLE_USERS_P1 = {"kind": "admin#directory#users", "nextPageToken": "p2", "users": [
    {"id": "u1", "primaryEmail": "dana.reyes@acme.io", "name": {"fullName": "Dana Reyes"}, "isAdmin": True,
     "suspended": False, "lastLoginTime": "2026-11-12T09:30:00.000Z"},
    {"id": "u2", "primaryEmail": "Alex.Chen@acme.io", "name": {"fullName": "Alex Chen"}, "isAdmin": False,
     "suspended": True, "lastLoginTime": "1970-01-01T00:00:00.000Z"},
]}
GOOGLE_USERS_P2 = {"kind": "admin#directory#users", "users": [
    {"id": "u3", "primaryEmail": "sam.ortiz@acme.io", "name": {"fullName": "Sam Ortiz"}, "isAdmin": False,
     "suspended": False, "lastLoginTime": "2026-11-01T12:00:00.000Z"},
]}
GOOGLE_ROLES = {"items": [
    {"roleId": "9170516996784129", "roleName": "_SEED_ADMIN_ROLE", "isSuperAdminRole": True, "isSystemRole": True},
    {"roleId": "9170516996784130", "roleName": "_GROUPS_ADMIN_ROLE", "isSystemRole": True},
    {"roleId": "9170516996784131", "roleName": "_HELP_DESK_ADMIN_ROLE", "isSystemRole": True},
]}
GOOGLE_ASSIGNMENTS = {"items": [
    {"roleAssignmentId": "a1", "roleId": "9170516996784130", "assignedTo": "u2", "assigneeType": "user",
     "scopeType": "CUSTOMER"},
    {"roleAssignmentId": "a2", "roleId": "9170516996784131", "assignedTo": "g1", "assigneeType": "group",
     "scopeType": "CUSTOMER"},
]}


def google_handler(request):
    url = str(request.url)
    if url.startswith("https://oauth2.googleapis.com/token"):
        form = parse_qs(request.content.decode())
        claims = jwt.decode(form["assertion"][0], PUBLIC_PEM, algorithms=["RS256"],
                            audience="https://oauth2.googleapis.com/token")
        assert claims["sub"] == "it-admin@acme.io" and claims["iss"] == SERVICE_ACCOUNT["client_email"]
        assert "readonly" in claims["scope"] and "admin.directory.user.readonly" in claims["scope"]
        return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3599, "token_type": "Bearer"})
    assert request.headers["Authorization"] == "Bearer ya29.test"
    path = request.url.path
    if path.endswith("/users"):
        return httpx.Response(200, json=GOOGLE_USERS_P2 if _q(request).get("pageToken") == "p2" else GOOGLE_USERS_P1)
    if path.endswith("/roles"):
        return httpx.Response(200, json=GOOGLE_ROLES)
    if path.endswith("/roleassignments"):
        return httpx.Response(200, json=GOOGLE_ASSIGNMENTS)
    return httpx.Response(404, json={"error": {"message": "not found"}})


GOOGLE_CONFIG = {"admin_email": "IT-Admin@acme.io"}
GOOGLE_SECRET = {"service_account": SERVICE_ACCOUNT}


def test_google_validate():
    cfg = google_workspace.validate(GOOGLE_CONFIG, GOOGLE_SECRET)
    assert cfg == {"admin_email": "it-admin@acme.io", "service_account_email": SERVICE_ACCOUNT["client_email"],
                   "domain": "acme.io"}
    with pytest.raises(ConnectorError, match="service account key"):
        google_workspace.validate(GOOGLE_CONFIG, {"service_account": {"client_email": "x"}})
    with pytest.raises(ConnectorError, match="admin"):
        google_workspace.validate({}, GOOGLE_SECRET)


def test_google_pull(mock_http):
    mock_http.handler = google_handler
    cfg = google_workspace.validate(GOOGLE_CONFIG, GOOGLE_SECRET)
    pulled = google_workspace.pull(cfg, GOOGLE_SECRET)
    rows = {(r["email"], r["role"]): r for r in pulled.rows}
    assert set(rows) == {("dana.reyes@acme.io", "Super Admin"), ("alex.chen@acme.io", "Groups Admin"),
                         ("sam.ortiz@acme.io", "")}
    alex = rows[("alex.chen@acme.io", "Groups Admin")]
    assert alex["status"] == "inactive" and alex["last_login"] is None  # suspended, never signed in
    assert rows[("dana.reyes@acme.io", "Super Admin")]["last_login"] == "2026-11-12T09:30:00.000Z"
    assert pulled.application == "Google Workspace"
    assert any("assigned to groups" in w for w in pulled.warnings)
    raw = json.loads(pulled.raw_bytes())
    assert [p["request"] for p in raw["pages"]] == ["List users", "List users", "List admin roles",
                                                     "List admin role assignments"]


def test_google_refused_sign_in_explains_delegation(mock_http):
    mock_http.handler = lambda r: httpx.Response(401, json={"error": "unauthorized_client",
                                                            "error_description": "Client is unauthorized"})
    cfg = google_workspace.validate(GOOGLE_CONFIG, GOOGLE_SECRET)
    with pytest.raises(ConnectorError, match="domain-wide delegation"):
        google_workspace.test(cfg, GOOGLE_SECRET)


def test_role_labels():
    assert google_workspace.role_label("_SEED_ADMIN_ROLE") == "Super Admin"
    assert google_workspace.role_label("_USER_MANAGEMENT_ADMIN_ROLE") == "User Management Admin"
    assert google_workspace.role_label("Custom Billing Role") == "Custom Billing Role"


# ---------------------------------------------------------------------------
# Jira Cloud
# ---------------------------------------------------------------------------

JIRA_USERS = [
    {"accountId": "5b10a2844c20165700ede21g", "accountType": "atlassian", "emailAddress": "priya.shah@acme.io",
     "displayName": "Priya Shah", "active": True},
    {"accountId": "5b10ac8d82e05b22cc7d4ef5", "accountType": "atlassian", "emailAddress": "jordan.kim@acme.io",
     "displayName": "Jordan Kim", "active": True},
    {"accountId": "6c2b9e1a0d3f4a5b6c7d8e9f", "accountType": "atlassian", "displayName": "Marcus Lee",
     "active": True},  # email hidden by profile visibility
    {"accountId": "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077", "accountType": "app",
     "displayName": "Automation for Jira", "active": True},
    {"accountId": "qm:1234-portal", "accountType": "customer", "emailAddress": "someone@customer.example",
     "displayName": "Portal Customer", "active": True},
    {"accountId": "5b10a0effa615349cb016cd8", "accountType": "atlassian", "emailAddress": "alex.chen@acme.io",
     "displayName": "Alex Chen", "active": False},
]
JIRA_GROUPS = [{"name": "jira-administrators", "groupId": "g-admins"},
               {"name": "jira-software-users", "groupId": "g-users"}, {"name": "site-admins", "groupId": "g-site"}]
JIRA_MEMBERS = {
    "g-admins": ["5b10a2844c20165700ede21g"],
    "g-users": ["5b10a2844c20165700ede21g", "5b10ac8d82e05b22cc7d4ef5", "6c2b9e1a0d3f4a5b6c7d8e9f",
                "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077", "5b10a0effa615349cb016cd8"],
    "g-site": ["5b10ac8d82e05b22cc7d4ef5"],
}


def jira_handler(request, issues=None, fail_issue_after=None):
    assert request.headers["Authorization"].startswith("Basic ")
    path, q = request.url.path, _q(request)
    if path == "/rest/api/3/myself":
        return httpx.Response(200, json={"accountId": "x", "displayName": "IT Admin"})
    if path == "/rest/api/3/users/search":
        return httpx.Response(200, json=JIRA_USERS)
    if path == "/rest/api/3/group/bulk":
        start = int(q["startAt"])
        page = JIRA_GROUPS[:2] if start == 0 else JIRA_GROUPS[2:]
        return httpx.Response(200, json={"startAt": start, "maxResults": 50, "isLast": start > 0, "values": page})
    if path == "/rest/api/3/group/member":
        ids = JIRA_MEMBERS[q["groupId"]]
        assert q["includeInactiveUsers"] == "true"
        return httpx.Response(200, json={"isLast": True, "values": [{"accountId": i} for i in ids]})
    if path == "/rest/api/3/issue" and request.method == "POST":
        if issues is not None and fail_issue_after is not None and len(issues) >= fail_issue_after:
            return httpx.Response(400, json={"errorMessages": [], "errors": {"issuetype": "Specify a valid issue type"}})
        body = json.loads(request.content)
        if issues is not None:
            issues.append(body["fields"])
        return httpx.Response(201, json={"id": "10001", "key": f"IT-{len(issues or []) or 1}", "self": "x"})
    return httpx.Response(404, json={"errorMessages": ["not found"]})


JIRA_CONFIG = {"site": "https://Acme.atlassian.net/", "email": "it-admin@acme.io", "project_key": "it"}
JIRA_SECRET = {"api_token": JIRA_TOKEN}


def test_jira_validate():
    cfg = jira_cloud.validate(JIRA_CONFIG, JIRA_SECRET)
    assert cfg == {"site": "https://acme.atlassian.net", "email": "it-admin@acme.io", "project_key": "IT",
                   "issue_type": "Task"}
    assert jira_cloud.normalize_site("acme") == "https://acme.atlassian.net"
    for bad in ("", "evil.example.com", "acme.atlassian.net.evil.com", "https://x/y.atlassian.net"):
        with pytest.raises(ConnectorError):
            jira_cloud.normalize_site(bad)
    with pytest.raises(ConnectorError, match="API token"):
        jira_cloud.validate(JIRA_CONFIG, {})
    with pytest.raises(ConnectorError, match="Project keys"):
        jira_cloud.validate({**JIRA_CONFIG, "project_key": "1-bad"}, JIRA_SECRET)


def test_jira_pull(mock_http):
    mock_http.handler = jira_handler
    cfg = jira_cloud.validate(JIRA_CONFIG, JIRA_SECRET)
    pulled = jira_cloud.pull(cfg, JIRA_SECRET)
    rows = {(r["username"], r["role"]) for r in pulled.rows}
    assert rows == {
        ("priya.shah@acme.io", "jira-administrators"), ("priya.shah@acme.io", "jira-software-users"),
        ("jordan.kim@acme.io", "jira-software-users"), ("jordan.kim@acme.io", "site-admins"),
        ("Marcus Lee", "jira-software-users"), ("svc-Automation for Jira", "jira-software-users"),
        ("alex.chen@acme.io", "jira-software-users"),
    }  # the portal customer is skipped
    alex = next(r for r in pulled.rows if r["email"] == "alex.chen@acme.io")
    assert alex["status"] == "inactive" and alex["last_login"] is None
    assert any("hide their email" in w for w in pulled.warnings)
    assert all(str(c.url).startswith("https://acme.atlassian.net/") for c in mock_http.calls)


def test_jira_errors_are_plain(mock_http):
    cfg = jira_cloud.validate(JIRA_CONFIG, JIRA_SECRET)
    mock_http.handler = lambda r: httpx.Response(401, text="Unauthorized")
    with pytest.raises(ConnectorError, match="access denied"):
        jira_cloud.test(cfg, JIRA_SECRET)
    mock_http.handler = lambda r: httpx.Response(200, text="<html>login</html>")
    with pytest.raises(ConnectorError):
        jira_cloud.test(cfg, JIRA_SECRET)


def test_jira_create_ticket(mock_http):
    issues = []
    mock_http.handler = lambda r: jira_handler(r, issues)
    cfg = jira_cloud.validate(JIRA_CONFIG, JIRA_SECRET)
    key = jira_cloud.create_ticket(cfg, JIRA_SECRET, "Remove admin", "Line one\n\nLine two", labels=["risk-high"])
    assert key == "IT-1"
    f = issues[0]
    assert f["project"] == {"key": "IT"} and f["issuetype"] == {"name": "Task"}
    assert f["labels"] == ["access-review", "risk-high"] and "priority" not in f
    assert [p["content"][0]["text"] for p in f["description"]["content"]] == ["Line one", "Line two"]
    with pytest.raises(ConnectorError, match="project key"):
        jira_cloud.create_ticket({**cfg, "project_key": None}, JIRA_SECRET, "x", "y")


def test_catalog_knows_google_and_jira_admin_roles():
    tiers = {r: classify_privileged_access(r).risk_tier for r in (
        "Super Admin", "site-admins", "org-admins", "jira-administrators", "Groups Admin",
        "User Management Admin", "Help Desk Admin")}
    assert tiers == {"Super Admin": "critical", "site-admins": "critical", "org-admins": "critical",
                     "jira-administrators": "high", "Groups Admin": "high", "User Management Admin": "high",
                     "Help Desk Admin": "medium"}
    assert not classify_privileged_access("jira-software-users").is_privileged


# ---------------------------------------------------------------------------
# Through the API, with the database
# ---------------------------------------------------------------------------


@pytest.fixture
def secret_key(monkeypatch):
    monkeypatch.setenv("CONNECTOR_SECRET_KEY", Fernet.generate_key().decode())


def _add(api, kind, config, secret, name=None):
    return api.post("/connections", json={"kind": kind, "name": name, "config": config, "secret": secret})


def test_connections_need_a_server_key(api, monkeypatch):
    monkeypatch.delenv("CONNECTOR_SECRET_KEY", raising=False)
    r = _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET)
    assert r.status_code == 503 and "CONNECTOR_SECRET_KEY" in r.json()["detail"]


def test_connection_lifecycle_never_returns_secrets(api, secret_key, mock_http, db_engine):
    assert _add(api, "nope", {}, {}).status_code == 422
    assert _add(api, "jira_cloud", {**JIRA_CONFIG, "site": "evil.example.com"}, JIRA_SECRET).status_code == 422
    r = _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET, name="Acme Jira")
    assert r.status_code == 201, r.text
    conn = r.json()
    assert conn["status"] == "untested" and conn["can_create_tickets"] and conn["config"]["project_key"] == "IT"
    assert JIRA_TOKEN not in r.text and "secret" not in conn

    listed = api.get("/connections")
    assert listed.status_code == 200 and JIRA_TOKEN not in listed.text and len(listed.json()) == 1

    # Stored encrypted: the token isn't in the table in the clear. (A workspace id is its tenant id.)
    from sqlalchemy import select

    from backend.db.models import Connection
    from backend.db.session import tenant_session

    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"] if w["personal"])
    with tenant_session(uuid.UUID(wid), db_engine) as s:
        stored = s.scalar(select(Connection).where(Connection.id == uuid.UUID(conn["id"])))
        assert stored is not None and JIRA_TOKEN.encode() not in bytes(stored.secret)

    mock_http.handler = jira_handler
    r = api.post(f"/connections/{conn['id']}/test")
    assert r.status_code == 200 and r.json()["status"] == "ok" and "IT Admin" in r.json()["message"]

    # A failure is reported and its status is saved.
    mock_http.handler = lambda req: httpx.Response(401, text="Unauthorized")
    r = api.post(f"/connections/{conn['id']}/test")
    assert r.status_code == 422 and "access denied" in r.json()["detail"]
    saved = api.get("/connections").json()[0]
    assert saved["status"] == "error" and "access denied" in saved["last_error"]

    # Unknown ids are 404; removing works.
    assert api.post(f"/connections/{uuid.uuid4()}/test").status_code == 404
    assert api.delete(f"/connections/{conn['id']}").status_code == 200
    assert api.get("/connections").json() == []


def test_reviewers_cannot_manage_connections(api, secret_key):
    from tests.test_campaigns_db import _join_as

    _join_as(api, "rev@acme.io", "reviewer")
    assert api.get("/connections").status_code == 403
    assert _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET).status_code == 403


def _campaign_with_hr(api):
    cid = api.post("/campaigns", json={"name": "Connector test"}).json()["id"]
    r = api.post(f"/campaigns/{cid}/sources/hr", files={"file": ("hr.csv", (DATA / "sample_hr_roster.csv").read_bytes())})
    assert r.status_code == 201
    return cid


def test_pull_google_and_jira_into_a_campaign(api, secret_key, mock_http):
    google = _add(api, "google_workspace", GOOGLE_CONFIG, GOOGLE_SECRET).json()
    jira = _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET).json()
    assert PRIVATE_PEM[40:80] not in api.get("/connections").text
    cid = _campaign_with_hr(api)

    mock_http.handler = google_handler
    r = api.post(f"/campaigns/{cid}/sources/connection", json={"connection_id": google["id"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["snapshot"]["application"] == "Google Workspace" and body["snapshot"]["source"] == "google_workspace"
    assert body["counts"]["accounts"] == 3 and body["counts"]["entitlement_grants"] == 2
    assert not body["snapshot"]["needs_confirmation"]
    assert any("assigned to groups" in w for w in body["warnings"])

    mock_http.handler = jira_handler
    r = api.post(f"/campaigns/{cid}/sources/connection", json={"connection_id": jira["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["snapshot"]["application"] == "Jira" and r.json()["counts"]["accounts"] == 5

    # Identities: emails match; Marcus (hidden email) and the bot need a person to decide.
    items = api.post(f"/campaigns/{cid}/identities/resolve").json()["items"]
    for item in items:
        if item["status"] == "ambiguous":
            api.post(f"/campaigns/{cid}/identities/{item['account_id']}",
                     json={"person_id": item["candidates"][0]["person"]["id"]})
        elif item["status"] == "unmatched":
            api.post(f"/campaigns/{cid}/identities/{item['account_id']}", json={"account_type": "service"})
    r = api.post(f"/campaigns/{cid}/analyze")
    assert r.status_code == 200, r.text
    found = {(i["application"], i["native_id"], i["entitlement"]): i["severity"]
             for i in api.get(f"/campaigns/{cid}/review-items").json()["items"]}
    jordan = next(k for k in found if k[0] == "Jira" and k[2] == "site-admins")
    assert found[jordan] == "critical"  # terminated in HR, still a Jira site admin
    assert found[("Google Workspace", "dana.reyes@acme.io", "Super Admin")] == "high"
    # Alex left and is suspended in Google: a suspended account's admin role can't be used, so no finding.
    assert found[("Google Workspace", "alex.chen@acme.io", "Groups Admin")] == "none"

    ev = api.get(f"/campaigns/{cid}/evidence").json()
    pulled = [e for e in ev["events"] if e["type"] == "source.pulled"]
    assert [p["payload"]["kind"] for p in pulled] == ["google_workspace", "jira_cloud"]
    assert JIRA_TOKEN not in json.dumps(ev) and "private_key" not in json.dumps(ev)
    assert ev["verification"]["valid"]

    # The raw API responses are in the evidence pack, byte-for-byte as hashed.
    from tests.test_campaigns_db import _unzip
    api.post(f"/campaigns/{cid}/review/start")
    files = _unzip(api.post(f"/campaigns/{cid}/evidence-pack").content)
    raw = [n for n in files if n.startswith("population/") and "google_workspace" in n]
    assert raw and json.loads(files[raw[0]])["application"] == "Google Workspace"


def test_pull_failure_saves_status_and_loads_nothing(api, secret_key, mock_http):
    jira = _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET).json()
    cid = _campaign_with_hr(api)
    mock_http.handler = lambda req: httpx.Response(403, text="Forbidden")
    r = api.post(f"/campaigns/{cid}/sources/connection", json={"connection_id": jira["id"]})
    assert r.status_code == 422 and "access denied" in r.json()["detail"]
    assert len(api.get(f"/campaigns/{cid}").json()["snapshots"]) == 1
    assert api.get("/connections").json()[0]["status"] == "error"
    assert api.post(f"/campaigns/{cid}/sources/connection",
                    json={"connection_id": str(uuid.uuid4())}).status_code == 404


def test_network_failures_are_reported_not_500(api, secret_key, mock_http):
    jira = _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET).json()

    def unreachable(request):
        raise httpx.ConnectError("name resolution failed", request=request)

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    mock_http.handler = unreachable
    r = api.post(f"/connections/{jira['id']}/test")
    assert r.status_code == 422 and "Couldn't reach Jira Cloud" in r.json()["detail"]
    mock_http.handler = slow
    r = api.post(f"/connections/{jira['id']}/test")
    assert r.status_code == 422 and "didn't answer in time" in r.json()["detail"]
    assert api.get("/connections").json()[0]["status"] == "error"


def test_send_remediation_tickets_to_jira(api, secret_key, mock_http):
    cid = _signed_off(api)
    google = _add(api, "google_workspace", GOOGLE_CONFIG, GOOGLE_SECRET).json()
    jira = _add(api, "jira_cloud", JIRA_CONFIG, JIRA_SECRET).json()
    no_project = _add(api, "jira_cloud", {**JIRA_CONFIG, "project_key": ""}, JIRA_SECRET).json()
    assert not no_project["can_create_tickets"]

    assert api.post(f"/campaigns/{cid}/remediations/send-tickets",
                    json={"connection_id": google["id"]}).status_code == 422
    r = api.post(f"/campaigns/{cid}/remediations/send-tickets", json={"connection_id": no_project["id"]})
    assert r.status_code == 200 and "project key" in r.json()["result"]["error"]

    # The second ticket fails: the first is kept and recorded, the rest stay without a ticket.
    issues = []
    mock_http.handler = lambda req: jira_handler(req, issues, fail_issue_after=1)
    r = api.post(f"/campaigns/{cid}/remediations/send-tickets", json={"connection_id": jira["id"]})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert len(res["created"]) == 1 and "issue type" in res["error"]
    assert sum(1 for i in r.json()["remediations"]["items"] if i["ticket_ref"]) == 1

    mock_http.handler = lambda req: jira_handler(req, issues)
    r = api.post(f"/campaigns/{cid}/remediations/send-tickets", json={"connection_id": jira["id"]})
    res = r.json()["result"]
    assert len(res["created"]) == 2 and res["already_had_ticket"] == 1 and res["error"] is None
    refs = sorted(i["ticket_ref"] for i in r.json()["remediations"]["items"])
    assert refs == ["IT-1", "IT-2", "IT-3"]
    jordan = next(f for f in issues if "jordan.kim" in f["summary"])
    assert jordan["summary"] == "Remove AdministratorAccess for jordan.kim in AWS"
    assert "risk-critical" in jordan["labels"]

    # Nothing left to send.
    r = api.post(f"/campaigns/{cid}/remediations/send-tickets", json={"connection_id": jira["id"]})
    assert r.json()["result"]["created"] == [] and r.json()["result"]["already_had_ticket"] == 3

    ev = [e["type"] for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]]
    assert ev.count("remediation.ticket_sent") == 3

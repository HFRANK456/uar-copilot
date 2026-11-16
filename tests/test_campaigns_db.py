"""Phase 1 persistence tests: tenant isolation, append-only ledger, ingest, API."""
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from backend.db.models import (
    Account,
    AccountEntitlement,
    AccountType,
    Campaign,
    CampaignState,
    Entitlement,
    EvidenceEvent,
    Person,
)
from backend.db.session import tenant_session
from backend.domain import campaigns as cs
from backend.domain import ingest
from backend.domain.evidence import verify_chain
from tests.conftest import DATA

# The tests act as the campaign owner, who decides for reviewers with a recorded reason.
OVR = "Test run: owner deciding for reviewers"


def _new_campaign(engine, tenant_id, name="Q4 2026 UAR"):
    with tenant_session(tenant_id, engine) as s:
        return cs.create_campaign(s, tenant_id, "grc@acme.io", name).id


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_cannot_see_other_tenants_rows(db_engine, make_tenant):
    a, b = make_tenant("a"), make_tenant("b")
    cid = _new_campaign(db_engine, a)
    with tenant_session(b, db_engine) as s:
        assert s.scalar(select(Campaign).where(Campaign.id == cid)) is None
        assert s.scalars(select(EvidenceEvent)).all() == []
    with tenant_session(a, db_engine) as s:
        assert s.scalar(select(Campaign).where(Campaign.id == cid)) is not None


def test_tenant_cannot_write_into_other_tenant(db_engine, make_tenant):
    a, b = make_tenant(), make_tenant()
    with pytest.raises(DBAPIError, match="row-level security"):
        with tenant_session(b, db_engine) as s:
            s.add(Campaign(tenant_id=a, name="sneaky"))
            s.flush()


def test_no_tenant_set_sees_nothing(db_engine, make_tenant):
    a = make_tenant()
    _new_campaign(db_engine, a)
    with db_engine.connect() as conn:
        assert conn.execute(text("select count(*) from campaign")).scalar() == 0


def test_every_tenant_table_has_forced_rls(db_engine):
    from backend.db.models import RLS_TABLES

    with db_engine.connect() as conn:
        rows = conn.execute(
            text("select relname from pg_class where relrowsecurity and relforcerowsecurity and relname = any(:t)"),
            {"t": RLS_TABLES},
        ).scalars().all()
    assert sorted(rows) == sorted(RLS_TABLES)


# ---------------------------------------------------------------------------
# Evidence ledger
# ---------------------------------------------------------------------------


def test_evidence_chain_verifies_and_is_append_only(db_engine, make_tenant):
    t = make_tenant()
    cid = _new_campaign(db_engine, t)
    with tenant_session(t, db_engine) as s:
        c = cs.get_campaign(s, cid)
        cs.transition(s, c, CampaignState.ingesting, "grc@acme.io")
        result = verify_chain(s, cid)
    assert result["valid"] and result["events"] == 2

    with pytest.raises(DBAPIError, match="append-only"):
        with tenant_session(t, db_engine) as s:
            s.execute(text("update evidence_event set actor = 'mallory' where campaign_id = :c"), {"c": cid})
    with pytest.raises(DBAPIError, match="append-only"):
        with tenant_session(t, db_engine) as s:
            s.execute(text("delete from evidence_event where campaign_id = :c"), {"c": cid})


def test_tampering_breaks_the_chain(db_engine, make_tenant):
    t = make_tenant()
    cid = _new_campaign(db_engine, t)
    with tenant_session(t, db_engine) as s:
        cs.transition(s, cs.get_campaign(s, cid), CampaignState.ingesting, "grc@acme.io")
    # Simulate someone with owner rights bypassing the trigger, then roll back.
    with db_engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": str(t)})
        conn.execute(text("alter table evidence_event disable trigger evidence_event_append_only"))
        conn.execute(text("update evidence_event set actor = 'mallory' where campaign_id = :c and seq = 1"), {"c": cid})
        from sqlalchemy.orm import Session

        result = verify_chain(Session(bind=conn), cid)
        tx.rollback()
    assert result == {"valid": False, "events": 2, "broken_at_seq": 1}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_invalid_transition_is_rejected(db_engine, make_tenant):
    t = make_tenant()
    cid = _new_campaign(db_engine, t)
    with tenant_session(t, db_engine) as s:
        with pytest.raises(cs.InvalidTransition):
            cs.transition(s, cs.get_campaign(s, cid), CampaignState.closed, "grc@acme.io")


def test_period_end_before_start_rejected(db_engine, make_tenant):
    from datetime import date

    t = make_tenant()
    with tenant_session(t, db_engine) as s:
        with pytest.raises(ValueError):
            cs.create_campaign(s, t, "x", "bad", period_start=date(2026, 12, 31), period_end=date(2026, 10, 1))


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_ingest_hr_roster_and_access_list(db_engine, make_tenant):
    t = make_tenant()
    cid = _new_campaign(db_engine, t)
    hr = (DATA / "sample_hr_roster.csv").read_bytes()
    aws = (DATA / "sample_app_access_aws.csv").read_bytes()
    with tenant_session(t, db_engine) as s:
        c = cs.get_campaign(s, cid)
        r1 = ingest.ingest_hr_roster(s, c, hr, "sample_hr_roster.csv", "grc@acme.io")
        r2 = ingest.ingest_access_list(s, c, "AWS", aws, "sample_app_access_aws.csv", "grc@acme.io")
        assert c.state == CampaignState.ingesting

        assert r1.counts["people_created"] == 6 and r1.counts["managers_unresolved"] == 0  # the CEO has no manager
        mapped = {m.target: m.matched_column for m in r1.mappings}
        assert mapped["employee_id"] == "emp_id" and mapped["termination_date"] == "term_date"
        assert not any(m.needs_confirmation for m in r1.mappings)

        people = {p.employee_id: p for p in s.scalars(select(Person))}
        assert people["E102"].manager_id == people["E101"].id
        assert str(people["E103"].termination_date) == "2026-10-03"

        assert r2.counts == {
            "rows": 7, "accounts": 6, "entitlement_grants": 7, "entitlements": 5,
            "rows_skipped": 0, "service_or_shared_accounts": 1,
        }
        accts = {a.native_id: a for a in s.scalars(select(Account))}
        assert accts["svc-deploy"].account_type == AccountType.service
        assert accts["alex.chen"].status == "inactive"
        assert accts["priya.shah"].email == "priya.shah@acme.io"
        assert accts["priya.shah"].last_login_at is not None

        ents = {e.name: e for e in s.scalars(select(Entitlement))}
        assert ents["AdministratorAccess"].privilege_tier == "critical"
        assert s.scalar(select(text("count(*)")).select_from(AccountEntitlement)) == 7

        assert r1.snapshot.sha256 != r2.snapshot.sha256 and len(r2.snapshot.sha256) == 64
        assert verify_chain(s, cid)["valid"]


def test_ingest_rejects_unusable_files(db_engine, make_tenant):
    t = make_tenant()
    cid = _new_campaign(db_engine, t)
    with tenant_session(t, db_engine) as s:
        c = cs.get_campaign(s, cid)
        with pytest.raises(ingest.IngestError):
            ingest.ingest_hr_roster(s, c, b"foo,bar\n1,2\n", "x.csv", "a")
        with pytest.raises(ingest.IngestError):
            ingest.ingest_access_list(s, c, "App", b"", "x.csv", "a")
        with pytest.raises(ingest.IngestError):
            ingest.ingest_access_list(s, c, "", b"user_id\nu1\n", "x.csv", "a")
        assert c.state == CampaignState.draft  # nothing ingested, nothing changed


def test_xlsx_upload(db_engine, make_tenant, tmp_path):
    import pandas as pd

    path = tmp_path / "access.xlsx"
    pd.DataFrame({"login": ["a1", "a2"], "role": ["db_owner", "viewer"]}).to_excel(path, index=False)
    t = make_tenant()
    cid = _new_campaign(db_engine, t)
    with tenant_session(t, db_engine) as s:
        r = ingest.ingest_access_list(s, cs.get_campaign(s, cid), "SQL", path.read_bytes(), "access.xlsx", "a")
    assert r.counts["accounts"] == 2


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def api(db_engine, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.auth import require_auth
    from backend.main import app

    principal = {"sub": f"auth0|{uuid.uuid4()}", "email": "grc@acme.io"}
    app.dependency_overrides[require_auth] = lambda: principal
    client = TestClient(app)
    client.principal = principal
    yield client
    app.dependency_overrides.clear()


def test_api_campaign_flow(api):
    r = api.post("/campaigns", json={"name": "Q4 2026", "period_start": "2026-10-01", "period_end": "2026-12-31"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["state"] == "draft"

    files = {"file": ("hr.csv", (DATA / "sample_hr_roster.csv").read_bytes(), "text/csv")}
    r = api.post(f"/campaigns/{cid}/sources/hr", files=files)
    assert r.status_code == 201, r.text
    assert r.json()["campaign_state"] == "ingesting"

    files = {"file": ("aws.csv", (DATA / "sample_app_access_aws.csv").read_bytes(), "text/csv")}
    r = api.post(f"/campaigns/{cid}/sources/access", data={"application": "AWS"}, files=files)
    assert r.status_code == 201, r.text
    snap = r.json()["snapshot"]
    assert snap["application"] == "AWS" and snap["row_count"] == 7

    r = api.post(f"/campaigns/{cid}/sources/{snap['id']}/confirm-mapping")
    assert r.status_code == 200 and r.json()["mapping_confirmed_by"] == "grc@acme.io"

    body = api.get(f"/campaigns/{cid}").json()
    assert len(body["snapshots"]) == 2

    ev = api.get(f"/campaigns/{cid}/evidence").json()
    assert ev["verification"]["valid"] is True
    assert [e["type"] for e in ev["events"]] == [
        "campaign.created", "campaign.state_changed", "source.ingested", "source.ingested", "source.mapping_confirmed",
    ]
    assert [c["id"] for c in api.get("/campaigns").json()] == [cid]


def test_api_other_user_gets_404(api):
    cid = api.post("/campaigns", json={"name": "mine"}).json()["id"]
    api.principal["sub"] = f"auth0|{uuid.uuid4()}"  # a different user, so a different tenant
    assert api.get(f"/campaigns/{cid}").status_code == 404
    assert api.get("/campaigns").json() == []


def test_api_bad_upload_is_400_and_rolls_back(api):
    cid = api.post("/campaigns", json={"name": "x"}).json()["id"]
    files = {"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")}
    assert api.post(f"/campaigns/{cid}/sources/hr", files=files).status_code == 400
    assert api.get(f"/campaigns/{cid}").json()["state"] == "draft"


def test_campaign_routes_503_without_database(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.auth import require_auth
    from backend.main import app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    app.dependency_overrides[require_auth] = lambda: {"sub": "x"}
    try:
        assert TestClient(app).get("/campaigns").status_code == 503
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def _ingested_campaign(api):
    cid = api.post("/campaigns", json={"name": "Identity test"}).json()["id"]
    api.post(f"/campaigns/{cid}/sources/hr", files={"file": ("hr.csv", (DATA / "sample_hr_roster.csv").read_bytes())})
    for app, name in (("AWS", "sample_app_access_aws.csv"), ("GitHub", "sample_app_access_github.csv")):
        r = api.post(
            f"/campaigns/{cid}/sources/access", data={"application": app}, files={"file": (name, (DATA / name).read_bytes())}
        )
        assert r.status_code == 201, r.text
    return cid


def test_api_identity_resolution_flow(api):
    cid = _ingested_campaign(api)
    r = api.post(f"/campaigns/{cid}/identities/resolve")
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {(i["application"], i["native_id"]): i for i in body["items"]}

    # AWS: email matches for employees; the service account and the vendor contractor are set aside.
    for login in ("priya.shah", "marcus.lee", "jordan.kim", "alex.chen"):
        assert by_id[("AWS", login)]["status"] == "matched"
        assert by_id[("AWS", login)]["method"] == "email"
    assert by_id[("AWS", "svc-deploy")]["status"] == "non_human"
    assert by_id[("AWS", "contractor.ext")]["status"] == "external"

    # GitHub: employee ID links; username patterns queue for a human; unknown login is unmatched.
    assert by_id[("GitHub", "E104")]["person"]["name"] == "Sam Ortiz"
    assert by_id[("GitHub", "pshah")]["status"] == "ambiguous"
    assert by_id[("GitHub", "pshah")]["candidates"][0]["person"]["name"] == "Priya Shah"
    assert by_id[("GitHub", "dreyes")]["status"] == "ambiguous"
    assert by_id[("GitHub", "ghost-user")]["status"] == "unmatched"

    s = body["summary"]
    assert s["accounts"] == 11 and s["ready_for_analysis"] is False
    assert s["open"] == s.get("ambiguous", 0) + s.get("unmatched", 0)
    assert api.get(f"/campaigns/{cid}").json()["state"] == "resolving_identities"

    # A human resolves every open item.
    for item in body["items"]:
        if item["status"] == "ambiguous":
            pid = item["candidates"][0]["person"]["id"]
            r = api.post(f"/campaigns/{cid}/identities/{item['account_id']}", json={"person_id": pid})
            assert r.status_code == 200 and r.json()["method"] == "manual", r.text
        elif item["status"] == "unmatched":
            r = api.post(f"/campaigns/{cid}/identities/{item['account_id']}", json={"account_type": "external"})
            assert r.status_code == 200 and r.json()["status"] == "external"

    summary = api.get(f"/campaigns/{cid}/identities").json()["summary"]
    assert summary["ready_for_analysis"] is True and summary["open"] == 0

    # Re-running keeps human decisions.
    again = api.post(f"/campaigns/{cid}/identities/resolve").json()
    assert {i["native_id"]: i["method"] for i in again["items"]}["pshah"] == "manual"
    assert again["summary"]["ready_for_analysis"] is True

    ev = api.get(f"/campaigns/{cid}/evidence").json()
    types = [e["type"] for e in ev["events"]]
    assert types.count("identities.resolved") == 2 and "identity.confirmed" in types
    assert ev["verification"]["valid"] is True


def test_api_identity_validation(api):
    cid = api.post("/campaigns", json={"name": "empty"}).json()["id"]
    assert api.post(f"/campaigns/{cid}/identities/resolve").status_code == 409  # still draft

    cid = _ingested_campaign(api)
    item = api.post(f"/campaigns/{cid}/identities/resolve").json()["items"][0]
    url = f"/campaigns/{cid}/identities/{item['account_id']}"
    assert api.post(url, json={}).status_code == 422
    assert api.post(url, json={"person_id": str(uuid.uuid4())}).status_code == 404
    assert api.post(url, json={"account_type": "human"}).status_code == 422
    assert api.post(f"/campaigns/{cid}/identities/{uuid.uuid4()}", json={"account_type": "service"}).status_code == 404


def test_hr_only_or_access_only_cannot_resolve(api):
    cid = api.post("/campaigns", json={"name": "hr only"}).json()["id"]
    api.post(f"/campaigns/{cid}/sources/hr", files={"file": ("hr.csv", (DATA / "sample_hr_roster.csv").read_bytes())})
    r = api.post(f"/campaigns/{cid}/identities/resolve")
    assert r.status_code == 400 and "access list" in r.json()["detail"]


def test_api_people_list(api):
    cid = _ingested_campaign(api)
    people = api.get(f"/campaigns/{cid}/people").json()
    assert len(people) == 6 and people[0]["name"] == "Alex Chen"
    assert api.get(f"/campaigns/{uuid.uuid4()}/people").status_code == 404


# ---------------------------------------------------------------------------
# Analysis and review items
# ---------------------------------------------------------------------------


def _resolved_campaign(api):
    cid = _ingested_campaign(api)
    items = api.post(f"/campaigns/{cid}/identities/resolve").json()["items"]
    for item in items:
        if item["status"] == "ambiguous":
            api.post(f"/campaigns/{cid}/identities/{item['account_id']}",
                     json={"person_id": item["candidates"][0]["person"]["id"]})
        elif item["status"] == "unmatched":
            api.post(f"/campaigns/{cid}/identities/{item['account_id']}", json={"account_type": "external"})
    return cid


def test_analysis_requires_resolved_identities(api):
    cid = _ingested_campaign(api)
    assert api.post(f"/campaigns/{cid}/analyze").status_code == 409  # still ingesting
    api.post(f"/campaigns/{cid}/identities/resolve")
    r = api.post(f"/campaigns/{cid}/analyze")
    assert r.status_code == 409 and "identity decision" in r.json()["detail"]


def test_analysis_creates_findings_and_review_items(api):
    cid = _resolved_campaign(api)
    r = api.post(f"/campaigns/{cid}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    items = {(i["application"], i["native_id"], i["entitlement"]): i for i in body["items"]}

    # One review item per grant; accounts without a role get "Account access".
    assert body["summary"]["review_items"] == 12 == len(body["items"])
    assert ("GitHub", "ghost-user", "write") in items

    # Terminated Jordan Kim still holds AWS admin: critical, with the terminated + privileged rules.
    jordan = items[("AWS", "jordan.kim", "AdministratorAccess")]
    assert jordan["severity"] == "critical"
    rules = {f["rule_id"] for f in jordan["findings"]}
    assert {"terminated_privileged_active", "terminated_active", "privileged_access_critical"} <= rules
    assert jordan["person"]["name"] == "Jordan Kim" and jordan["reviewer"]["name"] == "Priya Shah"

    # Alex Chen is terminated but the AWS account is disabled: no termination finding.
    alex = items[("AWS", "alex.chen", "BillingReadOnly")]
    assert not any(f["rule_id"].startswith("terminated") for f in alex["findings"])

    # Priya holds AWS admin while employed: high, reviewed by her manager (the CEO).
    priya = items[("AWS", "priya.shah", "AdministratorAccess")]
    assert priya["severity"] == "high" and priya["reviewer"]["name"] == "Dana Reyes"

    # The vendor contractor: external and dormant (last login 2026-06-01).
    contractor = {f["rule_id"] for f in items[("AWS", "contractor.ext", "ReadOnlyAccess")]["findings"]}
    assert {"external_account_active", "dormant_account"} <= contractor

    # Nobody reviews their own access: the CEO has no manager, so her item is unassigned.
    assert items[("GitHub", "dreyes", "owner")]["reviewer"] is None

    # Account-level findings are stored once per account, not once per entitlement.
    findings = api.get(f"/campaigns/{cid}/findings").json()
    term = [f for f in findings if f["rule_id"] == "terminated_active"]
    assert len(term) == 1 and term[0]["inputs"]["application"] == "AWS" and term[0]["rule_version"] >= 1

    # Items are ordered most severe first; filtering works.
    sev_order = [i["severity"] for i in body["items"]]
    assert sev_order[0] == "critical" and sev_order[-1] == "none"
    crit = api.get(f"/campaigns/{cid}/review-items?severity=critical").json()["items"]
    assert [i["native_id"] for i in crit] == ["jordan.kim"]

    assert api.get(f"/campaigns/{cid}").json()["state"] == "analyzing"
    ev = api.get(f"/campaigns/{cid}/evidence").json()
    assert ev["events"][-1]["type"] == "analysis.completed" and ev["verification"]["valid"]
    assert ev["events"][-1]["payload"]["rule_set_version"] == body["summary"]["rule_set_version"]


def test_analysis_rerun_replaces_previous(api):
    cid = _resolved_campaign(api)
    first = api.post(f"/campaigns/{cid}/analyze").json()["summary"]
    second = api.post(f"/campaigns/{cid}/analyze").json()["summary"]
    assert first == second
    assert len(api.get(f"/campaigns/{cid}/review-items").json()["items"]) == first["review_items"]


def test_reviewer_selection_rules():
    import uuid as _u

    from backend.db.models import Application, Campaign, Person
    from backend.domain.analysis import _pick_reviewer

    ceo = Person(id=_u.uuid4(), name="CEO")
    vp = Person(id=_u.uuid4(), name="VP", manager_id=ceo.id)
    eng = Person(id=_u.uuid4(), name="Eng", manager_id=vp.id)
    owner = Person(id=_u.uuid4(), name="Owner")
    people = {p.id: p for p in (ceo, vp, eng, owner)}
    app = Application(name="AWS", owner_person_id=owner.id)
    no_owner = Application(name="X", owner_person_id=None)
    by_manager = Campaign(reviewer_rule="manager")
    by_owner = Campaign(reviewer_rule="app_owner")

    assert _pick_reviewer(by_manager, eng, app, people) == (vp.id, "manager")
    assert _pick_reviewer(by_owner, eng, app, people) == (owner.id, "app_owner")
    assert _pick_reviewer(by_manager, ceo, app, people) == (owner.id, "app_owner")
    assert _pick_reviewer(by_manager, ceo, no_owner, people) == (None, "unassigned")
    assert _pick_reviewer(by_manager, None, app, people) == (owner.id, "app_owner")
    # The app owner never reviews their own account.
    assert _pick_reviewer(by_owner, owner, app, people)[0] != owner.id


# ---------------------------------------------------------------------------
# Review workflow
# ---------------------------------------------------------------------------


def _join_as(api, email, role):
    """The current user (an owner) invites `email` to their workspace; that person then signs in
    with their own Auth0 account, accepts, and every later request in the test is theirs."""
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"] if w["personal"])
    r = api.post(f"/workspaces/{wid}/members", json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    api.principal.clear()
    api.principal.update({"sub": f"auth0|{uuid.uuid4()}", "email": email})
    assert api.post(f"/workspaces/{wid}/invitation/accept").status_code == 200
    api.headers["X-Workspace-Id"] = wid
    return wid


def _review_campaign(api):
    cid = _resolved_campaign(api)
    api.post(f"/campaigns/{cid}/analyze")
    return cid


def _items(api, cid, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return {(i["application"], i["native_id"], i["entitlement"]): i
            for i in api.get(f"/campaigns/{cid}/review-items?{q}").json()["items"]}


def test_decisions_need_review_started(api):
    cid = _review_campaign(api)
    item = next(iter(_items(api, cid).values()))
    r = api.post(f"/campaigns/{cid}/review-items/{item['id']}/decision", json={"action": "revoke"})
    assert r.status_code == 409
    assert api.post(f"/campaigns/{cid}/review/start").status_code == 200
    assert api.get(f"/campaigns/{cid}").json()["state"] == "in_review"
    assert api.post(f"/campaigns/{cid}/analyze").status_code == 409  # analysis is frozen
    assert api.post(f"/campaigns/{cid}/review/start").status_code == 409


def test_review_decision_rules(api):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    items = _items(api, cid)
    url = lambda key: f"/campaigns/{cid}/review-items/{items[key]['id']}/decision"  # noqa: E731
    jordan = ("AWS", "jordan.kim", "AdministratorAccess")

    # Keeping critical access needs a real justification.
    assert api.post(url(jordan), json={"override_reason": OVR, "action": "keep"}).status_code == 422
    assert api.post(url(jordan), json={"override_reason": OVR, "action": "keep", "justification": "ok"}).status_code == 422
    # Modify must say what changes.
    assert api.post(url(jordan), json={"override_reason": OVR, "action": "modify"}).status_code == 422
    # Revoke needs nothing.
    r = api.post(url(jordan), json={"override_reason": OVR, "action": "revoke"})
    assert r.status_code == 200 and r.json()["action"] == "revoke" and r.json()["actor"] == "grc@acme.io"

    # Changing a decision adds a new one; the latest wins.
    r = api.post(url(jordan), json={"override_reason": OVR, "action": "keep", "justification": "Break-glass account approved by CISO, ticket SEC-12"})
    assert r.status_code == 200
    after = _items(api, cid)[jordan]
    assert after["status"] == "keep" and after["decision"]["justification"].startswith("Break-glass")

    # Low-risk items can be kept without justification.
    pshah = ("GitHub", "pshah", "admin")
    assert api.post(url(pshah), json={"override_reason": OVR, "action": "keep"}).status_code == 200

    progress = api.get(f"/campaigns/{cid}/review-items").json()["progress"]
    assert progress["decided"] == 2 and progress["by_action"]["keep"] == 2 and not progress["complete"]

    ev = api.get(f"/campaigns/{cid}/evidence").json()
    decided = [e for e in ev["events"] if e["type"] == "review.decided"]
    assert [e["payload"]["action"] for e in decided] == ["revoke", "keep", "keep"]
    assert ev["verification"]["valid"]


def test_decisions_are_append_only(db_engine, api):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    item = _items(api, cid)[("AWS", "jordan.kim", "AdministratorAccess")]
    api.post(f"/campaigns/{cid}/review-items/{item['id']}/decision", json={"override_reason": OVR, "action": "revoke"})
    with pytest.raises(DBAPIError, match="append-only"):
        with tenant_session(_tenant_of(db_engine, cid), db_engine) as s:
            s.execute(text("update decision set action = 'keep'"))


def _tenant_of(engine, campaign_id):
    """Find which tenant owns a campaign (the tenant table has no RLS; campaigns do)."""
    from sqlalchemy.orm import Session as S

    from backend.db.models import Tenant

    with S(engine) as s:
        for t in s.scalars(select(Tenant)):
            with tenant_session(t.id, engine) as ts_:
                if ts_.scalar(select(Campaign.id).where(Campaign.id == campaign_id)):
                    return t.id
    raise AssertionError("tenant not found")


def test_bulk_keep_refuses_risky_items(api):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    items = list(_items(api, cid).values())
    risky = [i["id"] for i in items if i["severity"] in ("critical", "high", "medium")]
    safe = [i["id"] for i in items if i["severity"] in ("none", "low")]

    r = api.post(f"/campaigns/{cid}/review-items/bulk-keep", json={"override_reason": OVR, "item_ids": safe + risky[:1]})
    assert r.status_code == 422 and "one at a time" in r.json()["detail"]
    assert api.get(f"/campaigns/{cid}/review-items").json()["progress"]["decided"] == 0  # all-or-nothing

    r = api.post(f"/campaigns/{cid}/review-items/bulk-keep", json={"override_reason": OVR, "item_ids": safe})
    assert r.status_code == 200 and r.json()["decided"] == len(safe)
    progress = api.get(f"/campaigns/{cid}/review-items").json()["progress"]
    assert progress["pending"] == len(risky) == progress["high_risk_pending"] + sum(
        1 for i in items if i["severity"] == "medium")
    bulk = [e for e in api.get(f"/campaigns/{cid}/evidence").json()["events"] if e["type"] == "review.decided"]
    assert all(e["payload"]["bulk"] for e in bulk)


def test_self_review_is_blocked(api):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    _join_as(api, "priya.shah@acme.io", "reviewer")  # Priya signs in
    items = _items(api, cid)
    own = items[("AWS", "priya.shah", "AdministratorAccess")]
    assert own["is_own_access"] is True
    r = api.post(f"/campaigns/{cid}/review-items/{own['id']}/decision", json={"action": "revoke"})
    assert r.status_code == 403

    # Priya manages Marcus and Jordan, so those are hers to review.
    mine = _items(api, cid, mine="true")
    assert {k[1] for k in mine} == {"marcus.lee", "mlee", "jordan.kim"}
    assert all(i["is_mine"] for i in mine.values())

    # Bulk keep skips her own low-risk items instead of failing.
    pshah = items[("GitHub", "pshah", "admin")]
    r = api.post(f"/campaigns/{cid}/review-items/bulk-keep", json={"item_ids": [pshah["id"]]})
    assert r.status_code == 200 and r.json() == {"decided": 0, "skipped_own_access": [pshah["id"]]}


def test_reassign_and_app_owner(api):
    cid = _review_campaign(api)
    people = {p["name"]: p["id"] for p in api.get(f"/campaigns/{cid}/people").json()}
    items = _items(api, cid)
    unassigned_aws = [i for (app, _, _), i in items.items() if app == "AWS" and i["reviewer"] is None]
    assert unassigned_aws  # the contractor and the service account

    apps = {a["name"]: a for a in api.get(f"/campaigns/{cid}/applications").json()}
    assert apps["AWS"]["owner"] is None
    r = api.put(f"/campaigns/{cid}/applications/{apps['AWS']['id']}/owner", json={"person_id": people["Marcus Lee"]})
    assert r.status_code == 200 and r.json()["items_assigned"] == len(unassigned_aws)
    after = _items(api, cid)
    assert all(after[k]["reviewer"]["name"] == "Marcus Lee" for k in after if k[0] == "AWS" and items[k]["reviewer"] is None)
    assert api.get(f"/campaigns/{cid}/applications").json()[0]["owner"]["name"] == "Marcus Lee"

    # Reassign; a person can't be made reviewer of their own access.
    jordan = items[("AWS", "jordan.kim", "AdministratorAccess")]
    url = f"/campaigns/{cid}/review-items/{jordan['id']}/reassign"
    r = api.post(url, json={"person_id": people["Sam Ortiz"]})
    assert r.status_code == 200 and r.json()["reviewer"]["name"] == "Sam Ortiz"
    assert api.post(url, json={"person_id": people["Jordan Kim"]}).status_code == 403
    types = [e["type"] for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]]
    assert "application.owner_set" in types and "review.reassigned" in types


# ---------------------------------------------------------------------------
# Sign-off and evidence pack
# ---------------------------------------------------------------------------


def _decide_all(api, cid):
    for key, item in _items(api, cid).items():
        if item["severity"] == "critical":
            body = {"override_reason": OVR, "action": "revoke"}
        elif item["severity"] in ("high",):
            body = {"override_reason": OVR, "action": "keep", "justification": "On-call admin approved by CISO, SEC-12"}
        else:
            body = {"override_reason": OVR, "action": "keep"}
        r = api.post(f"/campaigns/{cid}/review-items/{item['id']}/decision", json=body)
        assert r.status_code == 200, r.text


def test_sign_off_requires_every_decision(api):
    cid = _review_campaign(api)
    assert api.post(f"/campaigns/{cid}/sign-off", json={}).status_code == 409  # not in review yet
    api.post(f"/campaigns/{cid}/review/start")
    r = api.post(f"/campaigns/{cid}/sign-off", json={})
    assert r.status_code == 422 and "still need a decision" in r.json()["detail"]

    _decide_all(api, cid)
    r = api.post(f"/campaigns/{cid}/sign-off", json={"statement": "Reviewed with IT, all decisions final."})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "remediating"  # Jordan's access was revoked, so changes are pending
    ev = api.get(f"/campaigns/{cid}/evidence").json()["events"]
    signed = next(e for e in ev if e["type"] == "campaign.signed_off")
    assert signed["payload"]["changes_to_remediate"] == 1
    assert api.post(f"/campaigns/{cid}/review-items/{_items(api, cid)[('AWS', 'jordan.kim', 'AdministratorAccess')]['id']}/decision",
                    json={"action": "keep", "justification": "changed my mind after sign-off"}).status_code == 409


def test_sign_off_with_everything_kept_closes(api):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    for item in _items(api, cid).values():
        body = {"override_reason": OVR, "action": "keep", "justification": "Business need confirmed with the manager"}
        assert api.post(f"/campaigns/{cid}/review-items/{item['id']}/decision", json=body).status_code == 200
    assert api.post(f"/campaigns/{cid}/sign-off", json={}).json()["state"] == "closed"


def _unzip(content):
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_evidence_pack_contents_and_verification(api):
    import csv
    import hashlib
    import io
    import json

    cid = _review_campaign(api)
    assert api.post(f"/campaigns/{cid}/review/start").status_code == 200
    _decide_all(api, cid)
    # Change one decision so the history shows a superseded row.
    pshah = _items(api, cid)[("GitHub", "pshah", "admin")]
    api.post(f"/campaigns/{cid}/review-items/{pshah['id']}/decision", json={"override_reason": OVR, "action": "revoke"})
    api.post(f"/campaigns/{cid}/sign-off", json={})

    r = api.post(f"/campaigns/{cid}/evidence-pack")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    files = _unzip(r.content)
    manifest = json.loads(files["manifest.json"])
    assert hashlib.sha256(files["manifest.json"]).hexdigest() == r.headers["x-manifest-sha256"]

    # 1. Every file matches the manifest.
    assert set(manifest["files"]) == set(files) - {"manifest.json"}
    for name, meta in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == meta["sha256"], name

    # Population files are the uploads, byte for byte.
    pop = {n: b for n, b in files.items() if n.startswith("population/")}
    assert len(pop) == 3
    assert (DATA / "sample_hr_roster.csv").read_bytes() in pop.values()

    # 2. Recompute the hash chain exactly as README.txt describes.
    prev = "0" * 64
    events = [json.loads(line) for line in files["evidence_events.jsonl"].decode().splitlines()]
    for e in events:
        body = {k: e[k] for k in ("campaign_id", "seq", "type", "payload", "actor", "actor_kind", "occurred_at")}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        assert e["prev_hash"] == prev
        prev = hashlib.sha256(prev.encode() + canonical).hexdigest()
        assert e["hash"] == prev, e["seq"]
    assert prev == manifest["evidence_chain"]["head_hash"] and manifest["evidence_chain"]["valid"]

    # 3. CSV content.
    def rows(name):
        return list(csv.DictReader(io.StringIO(files[name].decode())))

    items = rows("review_items.csv")
    assert len(items) == 12 and all(i["decision"] != "pending" for i in items)
    jordan = next(i for i in items if i["account"] == "jordan.kim")
    assert jordan["severity"] == "critical" and jordan["decision"] == "revoke" and jordan["reviewer"] == "Priya Shah"
    decisions = rows("decisions.csv")
    assert [d["superseded"] for d in decisions if d["account"] == "pshah"] == ["True", "False"]
    findings = rows("findings.csv")
    assert any(f["rule_id"] == "terminated_privileged_active" and json.loads(f["inputs"])["termination_date"] == "2026-10-03"
               for f in findings)
    assert len(rows("identities.csv")) == 11
    assert rows("column_mappings.csv")
    assert files["report.pdf"].startswith(b"%PDF")
    assert b"Signed off by" in files["README.txt"] and manifest["draft"] is False

    # The export itself is in the ledger, carrying the manifest hash.
    ev = api.get(f"/campaigns/{cid}/evidence").json()
    assert ev["events"][-1]["type"] == "evidence.exported"
    assert ev["events"][-1]["payload"]["manifest_sha256"] == r.headers["x-manifest-sha256"]
    assert ev["verification"]["valid"]


def test_evidence_pack_draft_and_gating(api):
    import json

    cid = _ingested_campaign(api)
    assert api.post(f"/campaigns/{cid}/evidence-pack").status_code == 409  # nothing analyzed yet
    cid = _review_campaign(api)
    r = api.post(f"/campaigns/{cid}/evidence-pack")
    assert r.status_code == 200
    files = _unzip(r.content)
    assert json.loads(files["manifest.json"])["draft"] is True
    assert b"DRAFT" in files["README.txt"]


def test_pdf_handles_non_latin_names():
    from backend.domain.export import _latin1

    assert _latin1("Zoë Ångström → done") == "Zoë Ångström -> done"
    assert _latin1("名前") == "??"


# ---------------------------------------------------------------------------
# Reviewer authority
# ---------------------------------------------------------------------------


def _started(api):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    return cid


def test_assigned_reviewer_decides_without_override(api):
    cid = _started(api)
    jordan = _items(api, cid)[("AWS", "jordan.kim", "AdministratorAccess")]
    assert jordan["reviewer"]["name"] == "Priya Shah"
    _join_as(api, "priya.shah@acme.io", "reviewer")  # the assigned reviewer signs in
    mine = _items(api, cid)[("AWS", "jordan.kim", "AdministratorAccess")]
    assert mine["is_mine"] and not mine["needs_override"] and mine["can_decide"]
    r = api.post(f"/campaigns/{cid}/review-items/{jordan['id']}/decision", json={"action": "revoke"})
    assert r.status_code == 200, r.text
    assert r.json()["as_assigned_reviewer"] is True and r.json()["override_reason"] is None
    ev = [e for e in api.get(f"/campaigns/{cid}/evidence").json()["events"] if e["type"] == "review.decided"][-1]
    assert ev["payload"]["as_assigned_reviewer"] is True and ev["payload"]["assigned_reviewer"] == "priya.shah@acme.io"


def test_owner_needs_override_reason_and_others_are_refused(api):
    cid = _started(api)
    items = _items(api, cid)
    jordan = items[("AWS", "jordan.kim", "AdministratorAccess")]
    url = f"/campaigns/{cid}/review-items/{jordan['id']}/decision"
    assert jordan["needs_override"] and jordan["can_decide"]  # the owner, not the reviewer

    r = api.post(url, json={"action": "revoke"})
    assert r.status_code == 422 and "override reason" in r.json()["detail"] and "Priya Shah" in r.json()["detail"]
    assert api.post(url, json={"action": "revoke", "override_reason": "short"}).status_code == 422
    r = api.post(url, json={"action": "revoke", "override_reason": "Priya on leave; approved by CISO"})
    assert r.status_code == 200
    assert r.json()["as_assigned_reviewer"] is False and r.json()["override_reason"].startswith("Priya on leave")

    # Someone who is neither the reviewer nor the owner can't decide or run the campaign.
    _join_as(api, "marcus.lee@acme.io", "reviewer")
    other = _items(api, cid)[("AWS", "priya.shah", "AdministratorAccess")]
    assert other["can_decide"] is False
    r = api.post(f"/campaigns/{cid}/review-items/{other['id']}/decision",
                 json={"action": "revoke", "override_reason": "I would like to decide this one"})
    assert r.status_code == 403 and "assigned reviewer (Dana Reyes)" in r.json()["detail"]
    people = {p["name"]: p["id"] for p in api.get(f"/campaigns/{cid}/people").json()}
    assert api.post(f"/campaigns/{cid}/review-items/{other['id']}/reassign",
                    json={"person_id": people["Sam Ortiz"]}).status_code == 403
    assert api.post(f"/campaigns/{cid}/sign-off", json={}).status_code == 403
    assert api.get(f"/campaigns/{cid}").json()["is_owner"] is False


def test_bulk_keep_by_owner_needs_one_reason(api):
    cid = _started(api)
    safe = [i["id"] for i in _items(api, cid).values() if i["severity"] in ("none", "low")]
    r = api.post(f"/campaigns/{cid}/review-items/bulk-keep", json={"item_ids": safe})
    assert r.status_code == 422 and "override reason" in r.json()["detail"]
    r = api.post(f"/campaigns/{cid}/review-items/bulk-keep", json={"item_ids": safe, "override_reason": OVR})
    assert r.status_code == 200 and r.json()["decided"] == len(safe)
    assert api.get(f"/campaigns/{cid}/review-items").json()["progress"]["decided_by_owner_override"] == len(safe)


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------


AWS_AFTER = (
    "UserName,Email Address,Account Status,Permission Set,Last Sign In\n"
    "priya.shah,priya.shah@acme.io,Enabled,AdministratorAccess,2026-11-20\n"
    "marcus.lee,marcus.lee@acme.io,Enabled,PowerUserAccess,2026-11-20\n"
    "marcus.lee,marcus.lee@acme.io,Enabled,ReadOnlyAccess,2026-11-20\n"
    "svc-deploy,,Enabled,DeployRole,2026-11-20\n"
    "alex.chen,alex.chen@acme.io,Disabled,BillingReadOnly,2026-11-07\n"
    "contractor.ext,j.doe@vendor.example,Enabled,ReadOnlyAccess,2026-06-01\n"
)  # jordan.kim removed; the contractor's revoke was NOT done


def _signed_off(api):
    cid = _started(api)
    _decide_all(api, cid)  # revokes the critical item (Jordan)
    contractor = _items(api, cid)[("AWS", "contractor.ext", "ReadOnlyAccess")]
    api.post(f"/campaigns/{cid}/review-items/{contractor['id']}/decision",
             json={"action": "revoke", "override_reason": OVR})
    ghost = _items(api, cid)[("GitHub", "ghost-user", "write")]
    api.post(f"/campaigns/{cid}/review-items/{ghost['id']}/decision",
             json={"action": "modify", "justification": "Downgrade to read access only", "override_reason": OVR})
    assert api.post(f"/campaigns/{cid}/sign-off", json={}).json()["state"] == "remediating"
    return cid


def test_remediation_lifecycle(api):
    import csv
    import io
    import json

    cid = _signed_off(api)
    rem = api.get(f"/campaigns/{cid}/remediations").json()
    assert rem["summary"]["total"] == 3 and rem["summary"]["open"] == 3 and not rem["summary"]["ready_to_close"]
    by_acct = {r["account"]: r for r in rem["items"]}
    assert by_acct["jordan.kim"]["action"] == "revoke" and by_acct["ghost-user"]["action"] == "modify"
    assert by_acct["ghost-user"]["change"] == "Downgrade to read access only"

    # Tickets CSV for Jira import.
    t = api.get(f"/campaigns/{cid}/remediations/tickets.csv")
    tickets = list(csv.DictReader(io.StringIO(t.text)))
    assert t.status_code == 200 and len(tickets) == 3
    jt = next(x for x in tickets if "jordan.kim" in x["Summary"])
    assert jt["Summary"] == "Remove AdministratorAccess for jordan.kim in AWS" and jt["Priority"] == "Highest"

    # Record the ticket and that IT made the change.
    r = api.post(f"/campaigns/{cid}/remediations/{by_acct['jordan.kim']['id']}",
                 json={"ticket_ref": "IT-101", "status": "done", "note": "Removed in IAM Identity Center"})
    assert r.status_code == 200
    assert {x["account"]: x for x in r.json()["items"]}["jordan.kim"]["status"] == "done"
    assert api.post(f"/campaigns/{cid}/remediations/{by_acct['jordan.kim']['id']}",
                    json={"status": "verified"}).status_code == 422  # can't self-verify

    # Can't close yet.
    r = api.post(f"/campaigns/{cid}/close")
    assert r.status_code == 422 and "3 remediation(s)" in r.json()["detail"]

    # Verify with a later AWS export: Jordan's access is gone, the contractor's isn't.
    aws = next(a for a in api.get(f"/campaigns/{cid}/applications").json() if a["name"] == "AWS")
    r = api.post(f"/campaigns/{cid}/remediations/verify", data={"application_id": aws["id"]},
                 files={"file": ("aws_after.csv", AWS_AFTER.encode())})
    assert r.status_code == 200, r.text
    res = {x["account"]: x for x in r.json()["result"]["results"]}
    assert res["jordan.kim"]["verified"] is True and res["contractor.ext"]["verified"] is False
    assert res["contractor.ext"]["detail"] == "entitlement still held"
    items = {x["account"]: x for x in r.json()["remediations"]["items"]}
    assert items["jordan.kim"]["status"] == "verified" and items["jordan.kim"]["verification_method"] == "reupload"
    assert items["contractor.ext"]["status"] == "open"

    # The verification upload doesn't become campaign data.
    assert len(api.get(f"/campaigns/{cid}/identities").json()["items"]) == 11

    # Accept the other two with reasons, then close.
    assert api.post(f"/campaigns/{cid}/remediations/{items['contractor.ext']['id']}/accept",
                    json={"reason": "ok"}).status_code == 422
    for acct in ("contractor.ext", "ghost-user"):
        r = api.post(f"/campaigns/{cid}/remediations/{items[acct]['id']}/accept",
                     json={"reason": "Vendor offboarded; confirmed by email from vendor, ticket IT-102"})
        assert r.status_code == 200
    assert api.get(f"/campaigns/{cid}/remediations").json()["summary"]["ready_to_close"] is True
    r = api.post(f"/campaigns/{cid}/close")
    assert r.status_code == 200 and r.json()["state"] == "closed"
    assert api.post(f"/campaigns/{cid}/remediations/{items['jordan.kim']['id']}/accept",
                    json={"reason": "too late, campaign closed"}).status_code == 409

    ev = [e["type"] for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]]
    for t_ in ("remediation.updated", "remediation.verification_upload", "remediation.accepted", "campaign.closed"):
        assert t_ in ev
    assert api.get(f"/campaigns/{cid}/evidence").json()["verification"]["valid"]

    # The evidence pack carries remediation and the verification upload.
    files = _unzip(api.post(f"/campaigns/{cid}/evidence-pack").content)
    rem_rows = list(csv.DictReader(io.StringIO(files["remediations.csv"].decode())))
    assert {r["account"]: r["status"] for r in rem_rows} == {
        "jordan.kim": "verified", "contractor.ext": "accepted", "ghost-user": "accepted"}
    assert any(n.endswith("aws_after.csv") for n in files)
    decisions = list(csv.DictReader(io.StringIO(files["decisions.csv"].decode())))
    assert all(d["decided_by_assigned_reviewer"] == "False" and d["override_reason"] == OVR for d in decisions)
    manifest = json.loads(files["manifest.json"])
    assert manifest["campaign"]["state"] == "closed"


def test_remediation_is_owner_only(api):
    cid = _signed_off(api)
    rid = api.get(f"/campaigns/{cid}/remediations").json()["items"][0]["id"]
    _join_as(api, "marcus.lee@acme.io", "reviewer")
    assert api.post(f"/campaigns/{cid}/remediations/{rid}", json={"status": "done"}).status_code == 403
    assert api.post(f"/campaigns/{cid}/close").status_code == 403


# ---------------------------------------------------------------------------
# Shared workspaces
# ---------------------------------------------------------------------------


def test_first_sign_in_gets_a_personal_workspace(api):
    body = api.get("/workspaces").json()
    assert len(body["workspaces"]) == 1 and body["invitations"] == []
    w = body["workspaces"][0]
    assert w["personal"] and w["role"] == "owner"
    assert api.get("/workspaces").json() == body  # idempotent


def test_invited_people_share_campaigns_with_their_role(api):
    cid = _review_campaign(api)
    owner = dict(api.principal)
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"] if w["personal"])
    for email, role in (("dana.reyes@acme.io", "reviewer"), ("auditor@cpa.example", "auditor")):
        assert api.post(f"/workspaces/{wid}/members", json={"email": email, "role": role}).status_code == 201
    # Inviting someone who is still only invited updates their role (and resends the email).
    again = api.post(f"/workspaces/{wid}/members", json={"email": "dana.reyes@acme.io", "role": "admin"})
    assert again.status_code == 201 and again.json()["role"] == "admin"
    api.post(f"/workspaces/{wid}/members", json={"email": "dana.reyes@acme.io", "role": "reviewer"})

    # Dana signs in: she sees the invitation, not the campaign, until she accepts.
    api.principal.clear(); api.principal.update({"sub": "auth0|dana", "email": "dana.reyes@acme.io"})
    mine = api.get("/workspaces").json()
    # Other tests' campaigns may also have invited the sample people, so look for this workspace.
    assert wid in [i["id"] for i in mine["invitations"]] and len(mine["workspaces"]) == 1
    api.headers["X-Workspace-Id"] = wid
    assert api.get("/campaigns").status_code == 403  # not a member yet
    assert api.post(f"/workspaces/{wid}/invitation/accept").status_code == 200
    assert [c["id"] for c in api.get("/campaigns").json()] == [cid]

    # As a reviewer she can't run the campaign...
    assert api.post("/campaigns", json={"name": "nope"}).status_code == 403
    assert api.post(f"/campaigns/{cid}/analyze").status_code == 403
    files = {"file": ("hr.csv", (DATA / "sample_hr_roster.csv").read_bytes())}
    assert api.post(f"/campaigns/{cid}/sources/hr", files=files).status_code == 403
    assert api.post(f"/campaigns/{cid}/evidence-pack").status_code == 403

    # ...but once the owner starts the review she decides the items assigned to her.
    api.principal.clear(); api.principal.update(owner)
    assert api.post(f"/campaigns/{cid}/review/start").status_code == 200
    api.principal.clear(); api.principal.update({"sub": "auth0|dana", "email": "dana.reyes@acme.io"})
    priya = _items(api, cid)[("AWS", "priya.shah", "AdministratorAccess")]
    assert priya["reviewer"]["name"] == "Dana Reyes" and priya["is_mine"] and not priya["needs_override"]
    r = api.post(f"/campaigns/{cid}/review-items/{priya['id']}/decision",
                 json={"action": "keep", "justification": "On-call admin, approved by CISO"})
    assert r.status_code == 200 and r.json()["as_assigned_reviewer"] is True
    jordan = _items(api, cid)[("AWS", "jordan.kim", "AdministratorAccess")]  # assigned to Priya
    assert api.post(f"/campaigns/{cid}/review-items/{jordan['id']}/decision",
                    json={"action": "revoke", "override_reason": "trying to decide for Priya"}).status_code == 403

    # The auditor can read and export, but not decide.
    api.principal.clear(); api.principal.update({"sub": "auth0|cpa", "email": "auditor@cpa.example"})
    assert api.post(f"/workspaces/{wid}/invitation/accept").status_code == 200
    assert api.get(f"/campaigns/{cid}").status_code == 200
    assert api.post(f"/campaigns/{cid}/evidence-pack").status_code == 200
    assert api.post(f"/campaigns/{cid}/review-items/{jordan['id']}/decision",
                    json={"action": "revoke"}).status_code == 403
    assert api.post(f"/workspaces/{wid}/members", json={"email": "x@y.z", "role": "owner"}).status_code == 403

    # The owner sees everyone.
    api.principal.clear(); api.principal.update(owner)
    members = {m["email"] or "owner": m for m in api.get(f"/workspaces/{wid}/members").json()}
    assert members["dana.reyes@acme.io"]["status"] == "active" and members["auditor@cpa.example"]["role"] == "auditor"


def test_non_members_and_bad_ids_are_refused(api):
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"])
    api.principal.clear(); api.principal.update({"sub": "auth0|stranger", "email": "stranger@evil.example"})
    for header in (wid, str(uuid.uuid4()), "not-a-uuid"):
        api.headers["X-Workspace-Id"] = header
        assert api.get("/campaigns").status_code == 403
    assert api.get(f"/workspaces/{wid}/members").status_code == 403
    assert api.post(f"/workspaces/{wid}/invitation/accept").status_code == 404  # no invitation for them


def test_invitation_matches_only_the_invited_email(api):
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"])
    api.post(f"/workspaces/{wid}/members", json={"email": "Sam.Ortiz@acme.io", "role": "admin"})
    api.principal.clear(); api.principal.update({"sub": "auth0|other", "email": "someone.else@acme.io"})
    assert api.get("/workspaces").json()["invitations"] == []
    api.principal.clear(); api.principal.update({"sub": "auth0|sam", "email": "sam.ortiz@acme.io"})
    mine = lambda: [i["role"] for i in api.get("/workspaces").json()["invitations"] if i["id"] == wid]  # noqa: E731
    assert mine() == ["admin"]  # other tests' workspaces may have invited the same sample people
    assert api.post(f"/workspaces/{wid}/invitation/decline").status_code == 200
    assert mine() == []


def test_member_management_rules(api):
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"])
    members = api.get(f"/workspaces/{wid}/members").json()
    me = next(m for m in members if m["is_you"])
    # The last owner can't leave or be demoted.
    assert api.delete(f"/workspaces/{wid}/members/{me['id']}").status_code == 422
    assert api.patch(f"/workspaces/{wid}/members/{me['id']}", json={"role": "admin"}).status_code == 422
    inv = api.post(f"/workspaces/{wid}/members", json={"email": "new.owner@acme.io", "role": "owner"}).json()
    assert api.patch(f"/workspaces/{wid}/members/{inv['id']}", json={"role": "reviewer"}).status_code == 200
    assert api.delete(f"/workspaces/{wid}/members/{inv['id']}").status_code == 200
    assert api.patch(f"/workspaces/{wid}", json={"name": "Acme Security"}).status_code == 200
    assert next(w for w in api.get("/workspaces").json()["workspaces"])["name"] == "Acme Security"


def test_several_workspaces_stay_separate(api):
    """An MSP-style user runs reviews for two clients; data never crosses."""
    personal = next(w["id"] for w in api.get("/workspaces").json()["workspaces"])
    client = api.post("/workspaces", json={"name": "Client B"}).json()
    assert client["role"] == "owner" and not client["personal"]
    api.headers["X-Workspace-Id"] = client["id"]
    cid_b = api.post("/campaigns", json={"name": "Client B Q4"}).json()["id"]
    api.headers["X-Workspace-Id"] = personal
    assert api.get("/campaigns").json() == [] and api.get(f"/campaigns/{cid_b}").status_code == 404
    del api.headers["X-Workspace-Id"]  # no header = personal workspace
    assert api.get("/campaigns").json() == []
    api.headers["X-Workspace-Id"] = client["id"]
    assert [c["id"] for c in api.get("/campaigns").json()] == [cid_b]
    assert {w["name"] for w in api.get("/workspaces").json()["workspaces"]} >= {"Client B"}

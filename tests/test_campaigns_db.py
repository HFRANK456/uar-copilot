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
    from backend.db.models import TENANT_TABLES

    with db_engine.connect() as conn:
        rows = conn.execute(
            text("select relname from pg_class where relrowsecurity and relforcerowsecurity and relname = any(:t)"),
            {"t": TENANT_TABLES},
        ).scalars().all()
    assert sorted(rows) == sorted(TENANT_TABLES)


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

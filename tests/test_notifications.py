"""Invitation, review-start and reminder emails, through a fake Resend (no network)."""
import json
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from backend import mailer
from backend.db.models import Campaign, WorkspaceMember
from backend.db.session import tenant_session
from tests.test_campaigns_db import _join_as, _review_campaign, _tenant_of, api  # noqa: F401  (api is a fixture)


@pytest.fixture
def outbox(monkeypatch):
    """Every email 'sent' lands here; set `outbox.fail = True` to make the service refuse."""
    class Box(list):
        fail = False

    box = Box()

    def handler(request):
        assert request.url == httpx.URL(mailer.API)
        assert request.headers["Authorization"] == "Bearer re_test"
        if box.fail:
            return httpx.Response(500, json={"message": "Internal error"})
        box.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"email-{len(box)}"})

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "transport_factory", lambda: httpx.MockTransport(handler))
    return box


def _to(box, email):
    return [m for m in box if email in m["to"]]


def test_invitation_is_emailed(api, outbox):
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"] if w["personal"])
    r = api.post(f"/workspaces/{wid}/members", json={"email": "New.Person@acme.io", "role": "reviewer"})
    assert r.status_code == 201 and r.json()["email_sent"] is True
    [m] = _to(outbox, "new.person@acme.io")
    assert "invited you" in m["text"] and "reviewer" in m["text"] and "https://uar-copilot.vercel.app" in m["text"]
    assert m["from"] == mailer.DEFAULT_FROM and "<a href=" in m["html"]


def test_invitation_still_works_without_email(api, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"] if w["personal"])
    r = api.post(f"/workspaces/{wid}/members", json={"email": "x@acme.io", "role": "auditor"})
    assert r.status_code == 201 and r.json()["email_sent"] is False
    assert "isn't set up" in r.json()["email_detail"]


def test_starting_the_review_emails_and_adds_reviewers(api, outbox, db_engine):
    cid = _review_campaign(api)
    r = api.post(f"/campaigns/{cid}/review/start")
    assert r.status_code == 200, r.text
    n = r.json()["notifications"]
    reviewers = {i["reviewer"]["email"] for i in r.json()["items"] if i["reviewer"]}
    assert n["emailed"] == len(reviewers) and n["email_configured"] and set(n["invited_to_workspace"]) == reviewers
    for email in reviewers:
        [m] = _to(outbox, email)
        assert f"/campaigns/{cid}" in m["text"] and "item(s)" in m["subject"]
        # Counts and a link only: no accounts, entitlements or findings in an email.
        for secret in ("jordan.kim", "AdministratorAccess", "terminated", "contractor.ext"):
            assert secret not in m["text"], secret

    tenant = _tenant_of(db_engine, cid)
    with tenant_session(tenant, db_engine) as s:
        members = {m.email: m for m in s.scalars(select(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant))}
    assert all(members[e].role.value == "reviewer" and members[e].status == "invited" for e in reviewers)
    ev = [e for e in api.get(f"/campaigns/{cid}/evidence").json()["events"] if e["type"] == "review.reviewers_notified"]
    assert ev and ev[0]["payload"]["emailed"] == len(reviewers)


def test_email_failure_never_blocks_the_review(api, outbox):
    outbox.fail = True
    cid = _review_campaign(api)
    r = api.post(f"/campaigns/{cid}/review/start")
    assert r.status_code == 200 and api.get(f"/campaigns/{cid}").json()["state"] == "in_review"
    assert r.json()["notifications"]["emailed"] == 0 and "refused" in r.json()["notifications"]["failed"][0]["reason"]


def test_reminders_and_escalation_to_the_manager(api, outbox, db_engine):
    cid = _review_campaign(api)
    assert api.post(f"/campaigns/{cid}/reminders").status_code == 422  # not in review yet
    api.post(f"/campaigns/{cid}/review/start")
    outbox.clear()

    r = api.post(f"/campaigns/{cid}/reminders")
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["reminded"] >= 1 and res["escalated"] == 0 and not res["overdue"]
    assert all("cc" not in m or not m["cc"] for m in outbox)
    assert all(m["subject"].startswith("Reminder:") for m in outbox)

    # Past the due date: the reviewer's manager from the HR roster is copied.
    tenant = _tenant_of(db_engine, cid)
    with tenant_session(tenant, db_engine) as s:
        s.scalar(select(Campaign).where(Campaign.id == cid)).due_date = date(2026, 1, 31)
    outbox.clear()
    res = api.post(f"/campaigns/{cid}/reminders").json()["result"]
    assert res["overdue"] and res["escalated"] >= 1
    priya = _to(outbox, "priya.shah@acme.io")[0]  # Priya reviews Jordan and Marcus; Dana is her manager
    assert priya["subject"].startswith("Overdue:") and priya["cc"] == ["dana.reyes@acme.io"]
    assert "January 31, 2026" in priya["text"] and "manager is copied" in priya["text"]

    types = [e["type"] for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]]
    assert types.count("review.reminders_sent") == 2


def test_reminders_need_email_and_a_manager(api, outbox, monkeypatch):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/review/start")
    monkeypatch.delenv("RESEND_API_KEY")
    r = api.post(f"/campaigns/{cid}/reminders")
    assert r.status_code == 422 and "RESEND_API_KEY" in r.json()["detail"]
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    _join_as(api, "rev2@acme.io", "reviewer")
    assert api.post(f"/campaigns/{cid}/reminders").status_code == 403

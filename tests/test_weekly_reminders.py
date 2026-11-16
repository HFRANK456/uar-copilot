"""The weekly reminder job: secret-protected trigger, six-day spacing, per-campaign switch."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.db.models import Campaign
from backend.db.session import tenant_session
from tests.test_campaigns_db import _join_as, _review_campaign, _tenant_of, api  # noqa: F401  (fixture)
from tests.test_notifications import outbox  # noqa: F401  (fixture)


def _started(api):
    cid = _review_campaign(api)
    assert api.post(f"/campaigns/{cid}/review/start").status_code == 200
    return cid


def _age(db_engine, cid, days):
    """Pretend the last reminder went out `days` ago."""
    tenant = _tenant_of(db_engine, cid)
    with tenant_session(tenant, db_engine) as s:
        s.scalar(select(Campaign).where(Campaign.id == cid)).last_reminder_at = \
            datetime.now(timezone.utc) - timedelta(days=days)


def _weekly_events(api, cid):
    return [e for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]
            if e["type"] == "review.reminders_sent" and e["payload"].get("scheduled")]


def _run(api, secret="cron-test"):
    return api.post("/internal/weekly-reminders", headers={"X-Cron-Secret": secret})


def test_the_trigger_needs_the_secret(api, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert _run(api).status_code == 503
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    assert _run(api, "wrong").status_code == 401
    assert api.post("/internal/weekly-reminders").status_code == 401


def test_weekly_run_reminds_due_campaigns_only(api, outbox, db_engine, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    cid = _started(api)
    assert api.get(f"/campaigns/{cid}").json()["last_reminder_at"]  # review start counts as the first nudge

    r = _run(api)
    assert r.status_code == 200 and r.json()["ran"] is True
    assert _weekly_events(api, cid) == []  # emailed at review start, so not due yet

    _age(db_engine, cid, 7)
    outbox.clear()
    assert _run(api).status_code == 200
    [ev] = _weekly_events(api, cid)
    assert ev["actor"] == "system:weekly-reminders" and ev["actor_kind"] == "system"
    assert ev["payload"]["reminded"] >= 1
    assert any(f"/campaigns/{cid}" in m["text"] for m in outbox)

    assert _run(api).status_code == 200
    assert len(_weekly_events(api, cid)) == 1  # not again within six days
    assert api.get(f"/campaigns/{cid}/evidence").json()["verification"]["valid"]


def test_owner_can_turn_weekly_reminders_off(api, outbox, db_engine, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    cid = _started(api)
    r = api.post(f"/campaigns/{cid}/auto-reminders", json={"enabled": False})
    assert r.status_code == 200 and r.json()["auto_reminders"] is False
    _age(db_engine, cid, 30)
    _run(api)
    assert _weekly_events(api, cid) == []
    types = [e["type"] for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]]
    assert "review.auto_reminders_changed" in types

    _join_as(api, "rev3@acme.io", "reviewer")
    assert api.post(f"/campaigns/{cid}/auto-reminders", json={"enabled": True}).status_code == 403


def test_without_email_the_job_does_nothing(api, monkeypatch, db_engine):
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    cid = _started(api)
    _age(db_engine, cid, 30)
    r = _run(api)
    assert r.status_code == 200 and r.json() == {"ran": False, "reason": "email isn't set up on this server"}
    assert _weekly_events(api, cid) == []

"""Email tickets use the existing remediation workflow, with no real email delivery."""
import json

import httpx
import pytest

from backend import mailer
from tests.test_campaigns_db import _join_as, _review_campaign, _signed_off, api  # noqa: F401


@pytest.fixture
def ticket_outbox(monkeypatch):
    class Box(list):
        fail_after = None

    box = Box()

    def handler(request):
        if box.fail_after is not None and len(box) >= box.fail_after:
            return httpx.Response(500, json={"message": "Service unavailable"})
        box.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"ticket-{len(box)}"})

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "transport_factory", lambda: httpx.MockTransport(handler))
    return box


def _send(api, cid, recipient="IT@acme.io"):
    return api.post(f"/campaigns/{cid}/remediations/email-tickets", json={"recipient": recipient})


def test_email_tickets_records_delivery_and_skips_existing(api, ticket_outbox):
    cid = _signed_off(api)
    ticket_outbox.clear()  # review-start notifications aren't tickets
    r = _send(api, cid)
    assert r.status_code == 200, r.text
    assert len(r.json()["result"]["created"]) == len(ticket_outbox) == 3
    assert all(m["to"] == ["it@acme.io"] for m in ticket_outbox)
    modify = next(m for m in ticket_outbox if "ghost-user" in m["subject"])
    assert "Downgrade to read access only" in modify["text"]
    assert f"/campaigns/{cid}" in modify["text"] and "Remediation ID:" in modify["text"]
    items = r.json()["remediations"]["items"]
    assert all(i["ticket_ref"].startswith("email:ticket-") and i["status"] == "open" for i in items)
    assert _send(api, cid).json()["result"] == {"created": [], "already_had_ticket": 3, "error": None}
    assert len(ticket_outbox) == 3
    events = api.get(f"/campaigns/{cid}/evidence").json()
    deliveries = [e for e in events["events"] if e["type"] == "remediation.ticket_emailed"]
    assert len(deliveries) == 3 and all(e["payload"]["recipient"] == "it@acme.io" for e in deliveries)


def test_email_tickets_partial_failure_can_retry(api, ticket_outbox):
    cid = _signed_off(api)
    ticket_outbox.clear()
    ticket_outbox.fail_after = 1
    first = _send(api, cid).json()
    assert len(first["result"]["created"]) == 1 and "refused" in first["result"]["error"]
    assert sum(bool(i["ticket_ref"]) for i in first["remediations"]["items"]) == 1
    ticket_outbox.fail_after = None
    retry = _send(api, cid).json()["result"]
    assert len(retry["created"]) == 2 and retry["already_had_ticket"] == 1 and retry["error"] is None
    assert len(ticket_outbox) == 3
    events = api.get(f"/campaigns/{cid}/evidence").json()["events"]
    assert any(e["type"] == "remediation.ticket_email_failed" for e in events)


@pytest.mark.parametrize("recipient", ["bad", "a@b.com,c@d.com", "a@b.com\nBcc: x@y.com", "a" * 255 + "@b.com"])
def test_email_ticket_recipient_validation(api, ticket_outbox, recipient):
    cid = _signed_off(api)
    ticket_outbox.clear()
    assert _send(api, cid, recipient).status_code == 422
    assert not ticket_outbox


def test_email_tickets_permissions_state_and_configuration(api, ticket_outbox, monkeypatch):
    cid = _review_campaign(api)
    assert _send(api, cid).status_code == 409
    cid = _signed_off(api)
    monkeypatch.delenv("RESEND_API_KEY")
    assert _send(api, cid).status_code == 422
    _join_as(api, "reader@acme.io", "reviewer")
    assert _send(api, cid).status_code == 403


def test_email_tickets_skips_jira_and_resolved_changes(api, ticket_outbox):
    cid = _signed_off(api)
    ticket_outbox.clear()
    items = api.get(f"/campaigns/{cid}/remediations").json()["items"]
    base = f"/campaigns/{cid}/remediations"
    assert api.post(f"{base}/{items[0]['id']}", json={"ticket_ref": "KAN-42"}).status_code == 200
    assert api.post(f"{base}/{items[1]['id']}/accept", json={"reason": "IT verified this manually"}).status_code == 200
    r = _send(api, cid).json()["result"]
    assert len(r["created"]) == len(ticket_outbox) == 1 and r["already_had_ticket"] == 1

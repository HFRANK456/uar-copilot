"""Claude-drafted recommendations, with a fake Claude client (no API calls)."""
import csv
import io
import json
from types import SimpleNamespace

import pytest

from backend import llm
from tests.test_campaigns_db import OVR, _items, _review_campaign, _unzip, api  # noqa: F401  (api is a fixture)


class FakeClaude:
    """Stands in for anthropic.Anthropic(); `decide(items) -> list of suggestions` shapes the answer."""

    def __init__(self, decide=None, stop_reason="end_turn"):
        self.calls = []
        self.decide = decide or self.default
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    @staticmethod
    def default(items):
        out = []
        for it in items:
            sev = {f["severity"] for f in it["findings"]}
            action = "revoke" if "critical" in sev else "keep"
            out.append({"item": it["item"], "action": action, "confidence": "high" if action == "revoke" else "medium",
                        "rationale": f"Suggest {action} because of {it['findings'][0]['rule']}.",
                        "cited_findings": [it["findings"][0]["id"]]})
        return out

    def create(self, **kwargs):
        self.calls.append(kwargs)
        sent = json.loads(kwargs["messages"][0]["content"].split("\n", 1)[1])
        text = json.dumps({"recommendations": self.decide(sent)})
        return SimpleNamespace(stop_reason=self.stop_reason, model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def fake_claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(llm, "client_factory", lambda: fake)
    return fake


def test_needs_an_api_key(api, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cid = _review_campaign(api)
    r = api.post(f"/campaigns/{cid}/recommendations")
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_suggestions_are_drafted_validated_and_recorded(api, fake_claude):
    cid = _review_campaign(api)
    r = api.post(f"/campaigns/{cid}/recommendations")
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["items_sent"] == 5 and res["suggested"] == 5 and res["skipped"] == 0  # only flagged items

    # The request: the default model with fallbacks, a JSON schema, and nothing that identifies a person.
    call = fake_claude.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    sent = call["messages"][0]["content"]
    for secret in ("jordan", "Jordan", "priya", "@acme.io", "contractor.ext", "ghost-user", "vendor.example"):
        assert secret not in sent, secret
    assert "terminated_privileged_active" in sent and "Terminated" in sent  # the facts do go

    items = _items(api, cid)
    jordan = items[("AWS", "jordan.kim", "AdministratorAccess")]
    assert jordan["recommendation"] == "revoke"
    assert "terminated_privileged_active" in jordan["rationale"]
    meta = jordan["recommendation_meta"]
    assert meta["confidence"] == "high" and meta["model"] == "claude-opus-5-5"
    assert set(meta["cited_finding_ids"]) <= {f["id"] for f in jordan["findings"]}
    assert all(i["recommendation"] is None for i in items.values() if i["severity"] == "none")

    ev = api.get(f"/campaigns/{cid}/evidence").json()
    gen = next(e for e in ev["events"] if e["type"] == "recommendation.generated")
    assert gen["payload"]["items_sent"] == 5 and len(gen["payload"]["payload_sha256"]) == 64
    assert gen["payload"]["model_requested"] == "claude-opus-5-5" and len(gen["payload"]["suggestions"]) == 5
    assert ev["verification"]["valid"]


def test_answers_that_cite_the_wrong_item_or_nothing_are_dropped(api, monkeypatch):
    def decide(items):
        out = FakeClaude.default(items)
        out[0]["cited_findings"] = [items[1]["findings"][0]["id"]]  # another item's finding
        out[1]["cited_findings"] = []                                # cites nothing
        out.append({**out[2], "item": "I99"})                        # an item that wasn't sent
        return out

    fake = FakeClaude(decide)
    monkeypatch.setattr(llm, "client_factory", lambda: fake)
    cid = _review_campaign(api)
    res = api.post(f"/campaigns/{cid}/recommendations").json()["result"]
    assert res["suggested"] == 3 and res["skipped"] == 2
    assert sum(1 for i in _items(api, cid).values() if i["recommendation"]) == 3


def test_refusal_stores_nothing(api, monkeypatch):
    fake = FakeClaude(stop_reason="refusal")
    monkeypatch.setattr(llm, "client_factory", lambda: fake)
    cid = _review_campaign(api)
    r = api.post(f"/campaigns/{cid}/recommendations")
    assert r.status_code == 422 and "declined" in r.json()["detail"]
    assert all(i["recommendation"] is None for i in _items(api, cid).values())
    assert "recommendation.generated" not in [e["type"] for e in api.get(f"/campaigns/{cid}/evidence").json()["events"]]


def test_only_managers_and_only_between_analysis_and_sign_off(api, fake_claude):
    cid = api.post("/campaigns", json={"name": "Too early"}).json()["id"]
    r = api.post(f"/campaigns/{cid}/recommendations")
    assert r.status_code == 422 and "after analysis" in r.json()["detail"]

    from tests.test_campaigns_db import _join_as
    cid = _review_campaign(api)
    _join_as(api, "rev@acme.io", "reviewer")
    assert api.post(f"/campaigns/{cid}/recommendations").status_code == 403


def test_evidence_pack_shows_whether_people_followed_the_suggestion(api, fake_claude):
    cid = _review_campaign(api)
    api.post(f"/campaigns/{cid}/recommendations")
    api.post(f"/campaigns/{cid}/review/start")
    for key, item in _items(api, cid).items():
        if key == ("AWS", "jordan.kim", "AdministratorAccess"):
            body = {"action": "revoke", "override_reason": OVR}                     # follows "revoke"
        elif item["severity"] == "high":
            body = {"action": "revoke", "override_reason": OVR}                     # goes against "keep"
        else:
            body = {"action": "keep", "override_reason": OVR,
                    "justification": "Business need confirmed with the manager"}
        assert api.post(f"/campaigns/{cid}/review-items/{item['id']}/decision", json=body).status_code == 200
    rows = list(csv.DictReader(io.StringIO(
        _unzip(api.post(f"/campaigns/{cid}/evidence-pack").content)["review_items.csv"].decode())))
    by = {r["account"]: r for r in rows if r["ai_suggestion"]}
    assert by["jordan.kim"]["ai_suggestion"] == "revoke" and by["jordan.kim"]["ai_suggestion_followed"] == "True"
    high = next(r for r in rows if r["severity"] == "high")
    assert high["ai_suggestion"] == "keep" and high["ai_suggestion_followed"] == "False"
    assert all(r["ai_suggestion"] == "" for r in rows if r["severity"] == "none")

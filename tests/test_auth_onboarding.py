"""A reviewer can verify their email without restarting the server or logging out."""
import io
import json
import urllib.error

import pytest

from backend import auth


@pytest.fixture
def identity(monkeypatch):
    auth._USERINFO_CACHE.clear()
    settings = auth.Auth0Settings(True, "example.auth0.com", "api", "https://example.auth0.com/", ("RS256",))
    profile = {"sub": "new-reviewer", "email": "reviewer@example.com", "email_verified": False}
    calls = []

    def urlopen(req, timeout):
        calls.append(req)
        return io.BytesIO(json.dumps(profile).encode())

    monkeypatch.setattr(auth.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(auth, "_fetch_jwks", lambda _: {"keys": [{"kid": "test"}]})
    monkeypatch.setattr(auth.jwt, "get_unverified_header", lambda _: {"kid": "test"})
    monkeypatch.setattr(auth.jwt, "decode", lambda *a, **kw: {"sub": "new-reviewer"})
    yield settings, profile, calls
    auth._USERINFO_CACHE.clear()


def test_verification_recovers_on_next_request_with_same_token(identity):
    settings, profile, calls = identity
    assert "email" not in auth.verify_auth0_token("token", settings)
    profile["email_verified"] = True
    assert auth.verify_auth0_token("token", settings)["email"] == profile["email"]
    assert len(calls) == 2
    auth.verify_auth0_token("token", settings)
    assert len(calls) == 2  # verified identities still use the cache


def test_new_token_refreshes_previously_cached_identity(identity):
    settings, profile, calls = identity
    profile["email_verified"] = True
    auth.verify_auth0_token("old-token", settings)
    profile["email"] = "updated@example.com"
    assert auth.verify_auth0_token("new-token", settings)["email"] == "updated@example.com"
    assert len(calls) == 2


@pytest.mark.parametrize("verified", [False, None, "true"])
def test_userinfo_requires_explicit_verification(identity, verified):
    settings, profile, calls = identity
    profile["email_verified"] = verified
    assert "email" not in auth.verify_auth0_token("token", settings)
    assert not auth._USERINFO_CACHE


def test_userinfo_failure_can_retry(identity, monkeypatch):
    settings, profile, calls = identity
    original = auth.urllib.request.urlopen

    def fail(*args, **kwargs):
        raise urllib.error.URLError("temporary outage")

    monkeypatch.setattr(auth.urllib.request, "urlopen", fail)
    assert "email" not in auth.verify_auth0_token("token", settings)
    monkeypatch.setattr(auth.urllib.request, "urlopen", original)
    profile["email_verified"] = True
    assert auth.verify_auth0_token("token", settings)["email"] == profile["email"]


def test_token_email_cannot_bypass_verification(identity, monkeypatch):
    settings, profile, _ = identity
    monkeypatch.setattr(auth.jwt, "decode", lambda *a, **kw: {
        "sub": "new-reviewer", "email": "victim@example.com", "email_verified": False})
    assert "email" not in auth.verify_auth0_token("token", settings)
    profile.update(sub="someone-else", email_verified=True)
    assert "email" not in auth.verify_auth0_token("token", settings)


from tests.test_campaigns_db import api  # noqa: E402,F401


def test_verified_reviewer_discovers_and_accepts_invitation_without_restart(api, identity):
    settings, profile, _ = identity
    wid = next(w["id"] for w in api.get("/workspaces").json()["workspaces"] if w["personal"])
    assert api.post(f"/workspaces/{wid}/members", json={
        "email": profile["email"], "role": "reviewer"}).status_code == 201
    from backend.main import app

    app.dependency_overrides[auth.require_auth] = lambda: auth.verify_auth0_token("token", settings)
    before = api.get("/workspaces").json()
    assert before["email_ready"] is False and before["invitations"] == []
    assert api.post(f"/workspaces/{wid}/invitation/accept").status_code == 403
    profile["email_verified"] = True
    after = api.get("/workspaces").json()
    assert after["email_ready"] is True and after["invitations"][0]["id"] == wid
    assert api.post(f"/workspaces/{wid}/invitation/accept").status_code == 200
    joined = api.get("/workspaces").json()
    assert any(w["id"] == wid and w["role"] == "reviewer" for w in joined["workspaces"])

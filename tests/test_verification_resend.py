import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.api import verification


@pytest.fixture
def resend(monkeypatch):
    from backend.main import app

    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("AUTH0_AUDIENCE", "api")
    monkeypatch.setenv("AUTH0_VERIFICATION_CLIENT_ID", "server-client")
    monkeypatch.setenv("AUTH0_VERIFICATION_CLIENT_SECRET", "server-secret")
    auth.get_auth0_settings.cache_clear()
    verification._attempts.clear()
    principal = {"sub": "auth0|reviewer"}
    calls = []
    failure = {"status": None}

    def handler(req):
        calls.append(req)
        if failure["status"]:
            return httpx.Response(failure["status"], json={"message": "server-secret private-provider-detail"})
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "management-token"})
        return httpx.Response(201, json={"id": "job-test"})

    monkeypatch.setattr(verification, "transport_factory", lambda: httpx.MockTransport(handler))
    app.dependency_overrides[auth.require_auth] = lambda: principal
    yield TestClient(app), principal, calls, failure
    app.dependency_overrides.clear()
    verification._attempts.clear()
    auth.get_auth0_settings.cache_clear()


def test_resend_uses_only_validated_subject_and_server_credentials(resend):
    client, _, calls, _ = resend
    r = client.post("/auth/verification-email", json={"user_id": "auth0|victim", "email": "victim@example.com"})
    assert r.status_code == 200 and r.json()["requested"]
    assert len(calls) == 2
    assert json.loads(calls[0].content)["client_secret"] == "server-secret"
    assert json.loads(calls[1].content) == {"user_id": "auth0|reviewer"}
    assert calls[1].headers["Authorization"] == "Bearer management-token"
    assert "server-secret" not in r.text and "management-token" not in r.text


def test_resend_cooldown_is_per_user(resend, monkeypatch):
    client, principal, calls, _ = resend
    clock = [100.0]
    monkeypatch.setattr(verification.time, "monotonic", lambda: clock[0])
    assert client.post("/auth/verification-email").status_code == 200
    r = client.post("/auth/verification-email")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert len(calls) == 2
    principal["sub"] = "auth0|other"
    assert client.post("/auth/verification-email").status_code == 200
    principal["sub"] = "auth0|reviewer"
    clock[0] += 61
    assert client.post("/auth/verification-email").status_code == 200


def test_verified_social_and_local_users_do_not_send(resend):
    client, principal, calls, _ = resend
    principal["email"] = "verified@example.com"
    assert client.post("/auth/verification-email").json()["requested"] is False
    principal.pop("email")
    principal["sub"] = "google-oauth2|reviewer"
    assert client.post("/auth/verification-email").status_code == 422
    principal.update(sub="auth0|reviewer", auth_disabled=True)
    assert client.post("/auth/verification-email").status_code == 422
    assert not calls


def test_resend_requires_configuration(resend, monkeypatch):
    client, _, calls, _ = resend
    monkeypatch.delenv("AUTH0_VERIFICATION_CLIENT_SECRET")
    assert client.post("/auth/verification-email").status_code == 503
    assert not calls


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_provider_failure_is_sanitized(resend, code):
    client, _, _, failure = resend
    failure["status"] = code
    r = client.post("/auth/verification-email")
    assert r.status_code == 502
    assert "server-secret" not in r.text and "private-provider-detail" not in r.text


def test_resend_requires_authentication(monkeypatch):
    from backend.main import app

    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("AUTH0_AUDIENCE", "api")
    auth.get_auth0_settings.cache_clear()
    assert TestClient(app).post("/auth/verification-email").status_code == 401
    auth.get_auth0_settings.cache_clear()


def test_job_failure_does_not_report_email_sent(resend, monkeypatch):
    client, _, _, _ = resend

    def handler(req):
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "management-token"})
        return httpx.Response(403, json={"message": "private-provider-detail"})

    monkeypatch.setattr(verification, "transport_factory", lambda: httpx.MockTransport(handler))
    r = client.post("/auth/verification-email")
    assert r.status_code == 502 and "private-provider-detail" not in r.text

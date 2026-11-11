import importlib

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, **env):
    for key in ["AUTH_DISABLED", "AUTH0_DOMAIN", "AUTH0_AUDIENCE", "AUTHO_DOMAIN", "AUTHO_AUDIENCE"]:
        monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import backend.auth
    import backend.main

    backend.auth.get_auth0_settings.cache_clear()
    importlib.reload(backend.main)
    return TestClient(backend.main.app)


def _files(data_dir, users="sample_users.csv", terms="sample_termination.csv"):
    return {
        "user_access": (users, (data_dir / users).read_bytes(), "text/csv"),
        "termination": (terms, (data_dir / terms).read_bytes(), "text/csv"),
    }


def test_health_is_public(monkeypatch):
    assert _client(monkeypatch).get("/health").json() == {"status": "ok"}


def test_upload_fails_closed_when_auth_unconfigured(monkeypatch, data_dir):
    r = _client(monkeypatch).post("/upload", files=_files(data_dir))
    assert r.status_code == 503


def test_upload_requires_token_when_configured(monkeypatch, data_dir):
    c = _client(monkeypatch, AUTH0_DOMAIN="example.auth0.com", AUTH0_AUDIENCE="https://api")
    assert c.post("/upload", files=_files(data_dir)).status_code == 401


def test_upload_rejects_garbage_token(monkeypatch, data_dir):
    c = _client(monkeypatch, AUTH0_DOMAIN="example.auth0.com", AUTH0_AUDIENCE="https://api")
    r = c.post("/upload", files=_files(data_dir), headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_cors_does_not_allow_wildcard(monkeypatch):
    c = _client(monkeypatch, CORS_ORIGINS="https://app.example.com")
    r = c.options(
        "/upload",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert r.headers.get("access-control-allow-origin") != "*"
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"
    ok = c.options(
        "/upload",
        headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert ok.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_upload_returns_findings_mappings_and_hashes(monkeypatch, data_dir):
    c = _client(monkeypatch, AUTH_DISABLED="true")
    r = c.post("/upload", files=_files(data_dir, "test_users_messy.csv", "test_termination_varied.csv"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["summary"]) == {"critical", "high", "medium", "low", "total"}
    assert body["summary"]["total"] == len(body["findings"])
    assert any(m["target"] == "user_id" and m["matched_column"] == "employee_id" for m in body["column_mappings"])
    assert [i["source"] for i in body["inputs"]] == ["user_access", "termination"]
    assert all(len(i["sha256"]) == 64 for i in body["inputs"])
    assert body["rule_set_version"]


def test_missing_user_id_is_400(monkeypatch, data_dir):
    c = _client(monkeypatch, AUTH_DISABLED="true")
    files = {
        "user_access": ("u.csv", b"foo,bar\n1,2\n", "text/csv"),
        "termination": ("t.csv", (data_dir / "sample_termination.csv").read_bytes(), "text/csv"),
    }
    assert c.post("/upload", files=files).status_code == 400


def test_cors_allows_put_for_app_owner(monkeypatch):
    c = _client(monkeypatch, CORS_ORIGINS="https://app.example.com")
    r = c.options(
        "/campaigns/x/applications/y/owner",
        headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "PUT"},
    )
    assert r.status_code == 200

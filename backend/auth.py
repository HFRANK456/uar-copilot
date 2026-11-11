from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import JWTError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Auth0Settings:
    enabled: bool
    domain: str
    audience: str
    issuer: str
    algorithms: tuple[str, ...]


def _normalize_domain(domain: str) -> str:
    domain = domain.strip()
    if domain.startswith("https://"):
        domain = domain[len("https://") :]
    if domain.startswith("http://"):
        domain = domain[len("http://") :]
    return domain.strip().strip("/")


@lru_cache(maxsize=1)
def get_auth0_settings() -> Auth0Settings:
    # Accept common env var typos ("AUTHO_*" using letter O) to reduce deploy friction.
    domain = (os.getenv("AUTH0_DOMAIN") or os.getenv("AUTHO_DOMAIN") or "").strip()
    audience = (os.getenv("AUTH0_AUDIENCE") or os.getenv("AUTHO_AUDIENCE") or "").strip()
    issuer = (os.getenv("AUTH0_ISSUER") or os.getenv("AUTHO_ISSUER") or "").strip()
    algorithms_env = os.getenv("AUTH0_ALGORITHMS", "RS256").strip()
    algorithms = tuple(a.strip() for a in algorithms_env.split(",") if a.strip())

    enabled = bool(domain and audience)
    if enabled:
        domain_norm = _normalize_domain(domain)
        if not issuer:
            issuer = f"https://{domain_norm}/"
        domain = domain_norm

    return Auth0Settings(
        enabled=enabled,
        domain=domain,
        audience=audience,
        issuer=issuer,
        algorithms=algorithms or ("RS256",),
    )


_bearer = HTTPBearer(auto_error=False)


_JWKS_CACHE: dict[str, Any] = {"jwks": None, "fetched_at": 0.0}
_JWKS_TTL_SECONDS = 60 * 10


def _fetch_jwks(settings: Auth0Settings) -> Dict[str, Any]:
    now = time.time()
    cached = _JWKS_CACHE.get("jwks")
    fetched_at = float(_JWKS_CACHE.get("fetched_at") or 0.0)
    if cached and (now - fetched_at) < _JWKS_TTL_SECONDS:
        return cached

    url = f"https://{settings.domain}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=10) as resp:
        payload = resp.read()
    jwks = json.loads(payload.decode("utf-8"))
    _JWKS_CACHE["jwks"] = jwks
    _JWKS_CACHE["fetched_at"] = now
    return jwks


def _get_rsa_key(jwks: Dict[str, Any], kid: str) -> Optional[Dict[str, str]]:
    keys = jwks.get("keys") or []
    for key in keys:
        if key.get("kid") == kid:
            return {
                "kty": key.get("kty"),
                "kid": key.get("kid"),
                "use": key.get("use"),
                "n": key.get("n"),
                "e": key.get("e"),
            }
    return None


_USERINFO_CACHE: dict[str, tuple[Optional[str], float]] = {}
_USERINFO_TTL_SECONDS = 60 * 30


def _email_from_userinfo(token: str, settings: Auth0Settings, sub: str) -> Optional[str]:
    """Access tokens carry no email by default; ask Auth0 who this is (cached per user)."""
    cached = _USERINFO_CACHE.get(sub)
    if cached and time.time() - cached[1] < _USERINFO_TTL_SECONDS:
        return cached[0]
    email: Optional[str] = None
    try:
        req = urllib.request.Request(
            f"https://{settings.domain}/userinfo", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        if info.get("sub") == sub and info.get("email_verified", True):
            email = info.get("email")
    except Exception:  # identity still comes from the verified token; email is best effort
        logger.warning("userinfo_lookup_failed", extra={"sub": sub})
    _USERINFO_CACHE[sub] = (email, time.time())
    return email


def verify_auth0_token(token: str, settings: Auth0Settings) -> Dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token header.",
        ) from exc

    kid = header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token header missing kid.",
        )

    jwks = _fetch_jwks(settings)
    rsa_key = _get_rsa_key(jwks, kid)
    if not rsa_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to find matching JWKS key.",
        )

    try:
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=list(settings.algorithms),
            audience=settings.audience,
            issuer=settings.issuer,
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token validation failed.",
        ) from exc
    if not payload.get("email") and payload.get("sub"):
        email = _email_from_userinfo(token, settings, payload["sub"])
        if email:
            payload["email"] = email
    return payload


def auth_disabled() -> bool:
    """Explicit local-development opt-out. Never set this in a deployed environment."""
    return os.getenv("AUTH_DISABLED", "false").strip().lower() in {"1", "true", "yes"}


def require_auth(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Dict[str, Any]:
    """Fail-closed bearer-token check.

    Every protected route needs a valid Auth0 access token. The only way to skip
    verification is the explicit AUTH_DISABLED=true flag for local development.
    """
    if auth_disabled():
        return {"sub": "local-dev", "auth_disabled": True}

    settings = get_auth0_settings()
    if not settings.enabled:
        logger.error("auth_not_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on the server.",
        )

    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return verify_auth0_token(creds.credentials, settings)

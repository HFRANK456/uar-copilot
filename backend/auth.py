from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Auth0Settings:
    enabled: bool
    required: bool
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
    domain = os.getenv("AUTH0_DOMAIN", "").strip()
    audience = os.getenv("AUTH0_AUDIENCE", "").strip()
    issuer = os.getenv("AUTH0_ISSUER", "").strip()
    required = os.getenv("AUTH_REQUIRED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
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
        required=required,
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
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token validation failed.",
        ) from exc


def require_auth(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Dict[str, Any]:
    settings = get_auth0_settings()
    if not settings.enabled:
        if settings.required:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Auth is required but AUTH0 settings are not configured.",
            )
        return {}

    if creds is None or not creds.credentials:
        if settings.required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token.",
            )
        return {}

    return verify_auth0_token(creds.credentials, settings)


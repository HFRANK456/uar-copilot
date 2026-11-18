"""Authenticated, self-only resend of Auth0's verification email."""
import os
import threading
import time
from collections import OrderedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_auth0_settings, require_auth

router = APIRouter(prefix="/auth", tags=["authentication"])
transport_factory = lambda: None
_attempts = OrderedDict()
_lock = threading.Lock()
COOLDOWN = 60


@router.post("/verification-email")
def resend_verification(principal: dict = Depends(require_auth)):
    # The recipient is always Auth0's record for the validated token subject.
    # No email address, user ID or redirect URL is accepted from the browser.
    sub = principal.get("sub", "")
    if principal.get("email"):
        return {"requested": False, "detail": "Your email is already verified. Refresh your invitations."}
    if principal.get("auth_disabled") or not sub.startswith("auth0|"):
        raise HTTPException(422, "Use your email-and-password account to request verification.")
    settings = get_auth0_settings()
    client_id = (os.getenv("AUTH0_VERIFICATION_CLIENT_ID") or "").strip()
    secret = (os.getenv("AUTH0_VERIFICATION_CLIENT_SECRET") or "").strip()
    if not settings.enabled or not client_id or not secret:
        raise HTTPException(503, "Verification email resend isn't configured yet. Ask the workspace owner for help.")
    now = time.monotonic()
    with _lock:
        previous = _attempts.get(sub)
        if previous is not None and now - previous < COOLDOWN:
            raise HTTPException(429, "Please wait a minute before requesting another email.",
                                headers={"Retry-After": str(max(1, int(COOLDOWN - (now - previous))))})
        _attempts[sub] = now
        _attempts.move_to_end(sub)
        while len(_attempts) > 4096:
            _attempts.popitem(last=False)
    base = f"https://{settings.domain}"
    try:
        with httpx.Client(timeout=10, transport=transport_factory()) as client:
            token = client.post(base + "/oauth/token", json={
                "grant_type": "client_credentials", "client_id": client_id,
                "client_secret": secret, "audience": base + "/api/v2/",
            })
            token.raise_for_status()
            bearer = token.json()["access_token"]
            response = client.post(base + "/api/v2/jobs/verification-email",
                                   json={"user_id": sub}, headers={"Authorization": f"Bearer {bearer}"})
            response.raise_for_status()
    except (httpx.HTTPError, ValueError, KeyError):
        # Provider bodies can contain credentials or account data; never return them.
        raise HTTPException(502, "The identity service couldn't send the verification email. Try again in a minute.") from None
    return {"requested": True, "detail": "Verification email requested. Check your inbox and spam folder, then refresh verification here."}

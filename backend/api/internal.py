"""Endpoints for scheduled jobs, not people. No Auth0: they require the shared CRON_SECRET.

The weekly reminder job is triggered by a GitHub Actions schedule (.github/workflows/
weekly-reminders.yml). The backend runs on a plan that sleeps when idle, so an outside
trigger is more reliable than a timer inside the app, and the request also wakes it.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, status

from backend.db.session import get_engine
from backend.domain import notifications

router = APIRouter(prefix="/internal", tags=["internal"])
log = logging.getLogger("uar.internal")


def _check_secret(given: str | None) -> None:
    expected = (os.getenv("CRON_SECRET") or "").strip()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Scheduled jobs aren't enabled (CRON_SECRET is not set).")
    if not given or not hmac.compare_digest(given.strip(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong or missing X-Cron-Secret.")


@router.post("/weekly-reminders")
def weekly_reminders(x_cron_secret: str | None = Header(default=None)) -> Dict[str, Any]:
    _check_secret(x_cron_secret)
    engine = get_engine()
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No database configured.")
    result = notifications.run_weekly(engine)
    log.info("weekly reminders", extra=result)
    return result  # counts only: no names or addresses

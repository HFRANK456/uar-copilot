"""Outgoing email through Resend (https://resend.com), one HTTPS call per message.

Named `mailer`, not `email`: Render starts the app from inside backend/, where a module called
email.py would shadow Python's standard library.

Email is best-effort. Nothing in a campaign depends on a message arriving: when email isn't
configured or the service fails, `send` returns a result saying so, the caller records it,
and the app carries on (people can still be told by hand).

Settings:
  RESEND_API_KEY  required to send; without it every send reports "not set up".
  EMAIL_FROM      sender, e.g. "UAR Copilot <reviews@yourdomain.com>" (a domain verified in
                  Resend). Default is Resend's test sender, which only delivers to the address
                  that owns the Resend account.
  APP_URL         where links point. Default https://uar-copilot.vercel.app.
"""
from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

import httpx

API = "https://api.resend.com/emails"
DEFAULT_FROM = "UAR Copilot <onboarding@resend.dev>"
DEFAULT_APP_URL = "https://uar-copilot.vercel.app"
transport_factory: Callable[[], Optional[httpx.BaseTransport]] = lambda: None
log = logging.getLogger("uar.mailer")


@dataclass
class Result:
    sent: bool
    detail: str  # message id when sent, otherwise why not

    def as_dict(self) -> dict:
        return {"sent": self.sent, "detail": self.detail}


def configured() -> bool:
    return bool((os.getenv("RESEND_API_KEY") or "").strip())


def app_url(path: str = "") -> str:
    return (os.getenv("APP_URL") or DEFAULT_APP_URL).rstrip("/") + path


def _html(text: str) -> str:
    """Plain text to simple HTML: paragraphs, with bare links made clickable."""
    out = []
    for para in text.strip().split("\n\n"):
        lines = []
        for line in para.splitlines():
            safe = html.escape(line)
            if line.startswith("https://"):
                safe = f'<a href="{safe}">{safe}</a>'
            lines.append(safe)
        out.append("<p>" + "<br>".join(lines) + "</p>")
    return '<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5">' + "".join(out) + "</div>"


def send(to: List[str], subject: str, text: str, cc: Optional[List[str]] = None) -> Result:
    to = [t for t in to if t]
    if not to:
        return Result(False, "no recipient email address")
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        return Result(False, "email isn't set up on this server yet")
    body = {"from": os.getenv("EMAIL_FROM") or DEFAULT_FROM, "to": to, "subject": subject,
            "text": text, "html": _html(text)}
    if cc:
        body["cc"] = [c for c in cc if c and c not in to]
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), transport=transport_factory()) as c:
            resp = c.post(API, json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        log.warning("email failed", extra={"error": type(exc).__name__})
        return Result(False, "couldn't reach the email service")
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("message") or resp.text
        except ValueError:
            msg = resp.text
        log.warning("email rejected", extra={"status": resp.status_code})
        return Result(False, f"the email service refused it ({resp.status_code}: {msg[:200]})")
    return Result(True, (resp.json() or {}).get("id", "sent"))

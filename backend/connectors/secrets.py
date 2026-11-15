"""Encryption for connector credentials at rest.

Credentials are encrypted with Fernet (AES-128-CBC + HMAC) using CONNECTOR_SECRET_KEY,
which lives only in the backend's environment. They are decrypted only in memory, just
before calling the external API, and are never returned by the API or logged.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken


class SecretsUnavailable(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = (os.getenv("CONNECTOR_SECRET_KEY") or "").strip()
    if not key:
        raise SecretsUnavailable("Connections aren't enabled on this server yet (CONNECTOR_SECRET_KEY is not set).")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise SecretsUnavailable("CONNECTOR_SECRET_KEY is not a valid key.") from exc


def encrypt(data: Dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode("utf-8"))


def decrypt(token: bytes) -> Dict[str, Any]:
    try:
        return json.loads(_fernet().decrypt(bytes(token)).decode("utf-8"))
    except InvalidToken as exc:
        raise SecretsUnavailable("Stored credentials can't be decrypted (was CONNECTOR_SECRET_KEY changed?).") from exc

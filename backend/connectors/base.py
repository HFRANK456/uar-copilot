"""Common shape for connectors."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

# Tests swap this for an httpx.MockTransport that replays recorded responses.
transport_factory: Callable[[], Optional[httpx.BaseTransport]] = lambda: None  # noqa: E731

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_PAGES = 200  # hard stop so a misbehaving API can't loop forever


class ConnectorError(Exception):
    """A problem talking to the external system, phrased for the person who set it up."""


@dataclass
class AccessPull:
    """Canonical rows for load_access_frame, plus the raw responses for the snapshot."""

    application: str
    rows: List[Dict[str, Any]]
    raw_pages: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def raw_bytes(self) -> bytes:
        return json.dumps({"application": self.application, "pages": self.raw_pages},
                          sort_keys=True, indent=1, default=str).encode("utf-8")


def client(**kwargs: Any) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, transport=transport_factory(), **kwargs)


def check(resp: httpx.Response, what: str) -> Dict[str, Any] | List[Any]:
    if resp.status_code in (401, 403):
        raise ConnectorError(f"{what}: access denied ({resp.status_code}). Check the credentials and their permissions.")
    if resp.status_code == 404:
        raise ConnectorError(f"{what}: not found (404). Check the site or domain.")
    if resp.status_code == 429:
        raise ConnectorError(f"{what}: rate limited by the service; try again in a minute.")
    if resp.status_code >= 400:
        raise ConnectorError(f"{what}: the service returned {resp.status_code}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise ConnectorError(f"{what}: the service returned something that isn't JSON.") from exc

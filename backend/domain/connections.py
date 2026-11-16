"""Connections: a workspace's saved links to Google Workspace, Jira Cloud and later systems.

Credentials are encrypted at rest (see connectors.secrets) and never leave the server: the API
returns only the non-secret config. A pull fetches an application's accounts and entitlements
through the connector and loads them exactly like an uploaded access list, with the raw API
responses stored and hashed as the snapshot, so the evidence pack can show what was received.

Connector failures update the connection's status and are raised as ConnectorError, which
carries a plain-language message for the person who set it up. Callers commit that status
change (nothing else has been written when a connector call fails).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.connectors import google_workspace, jira_cloud, secrets
from backend.connectors.base import ConnectorError
from backend.db.models import Application, Campaign, Connection, SourceKind, SourceSnapshot
from backend.domain import remediation as remediation_service
from backend.domain.evidence import record_event
from backend.domain.ingest import IngestResult, load_access_frame
from schemas.user_schema import ColumnMapping

KINDS: Dict[str, ModuleType] = {"google_workspace": google_workspace, "jira_cloud": jira_cloud}
LABELS = {"google_workspace": "Google Workspace", "jira_cloud": "Jira Cloud"}
CANONICAL = ["native_id", "username", "email", "display_name", "status", "role", "last_login"]


def _out(c: Connection) -> Dict[str, Any]:
    return {"id": c.id, "kind": c.kind, "label": LABELS.get(c.kind, c.kind), "name": c.name,
            "config": c.config or {}, "status": c.status, "last_checked_at": c.last_checked_at,
            "last_error": c.last_error, "created_by": c.created_by, "created_at": c.created_at,
            "can_create_tickets": c.kind == "jira_cloud" and bool((c.config or {}).get("project_key"))}


def list_connections(session: Session) -> List[Dict[str, Any]]:
    return [_out(c) for c in session.scalars(select(Connection).order_by(Connection.created_at))]


def _get(session: Session, conn_id: uuid.UUID) -> Connection:
    c = session.scalar(select(Connection).where(Connection.id == conn_id))
    if c is None:
        raise LookupError("connection not found")
    return c


def create(session: Session, tenant_id: uuid.UUID, kind: str, name: Optional[str], config: Dict[str, Any],
           secret: Dict[str, Any], actor: str) -> Dict[str, Any]:
    module = KINDS.get(kind)
    if module is None:
        raise ConnectorError(f"Unknown connection type {kind!r}.")
    clean = module.validate(config or {}, secret or {})
    blob = secrets.encrypt(secret)  # fails clearly if the server has no encryption key
    c = Connection(tenant_id=tenant_id, kind=kind, name=(name or "").strip()[:255] or LABELS[kind],
                   config=clean, secret=blob, status="untested", created_by=actor)
    session.add(c)
    session.flush()
    return _out(c)


def delete(session: Session, conn_id: uuid.UUID) -> None:
    session.delete(_get(session, conn_id))
    session.flush()


def _run(c: Connection, fn):
    """Call the connector, recording success or the error on the connection."""
    c.last_checked_at = datetime.now(timezone.utc)
    try:
        try:
            result = fn(KINDS[c.kind], c.config or {}, secrets.decrypt(c.secret))
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"{LABELS[c.kind]} didn't answer in time. Try again in a minute.") from exc
        except httpx.HTTPError as exc:  # DNS, refused connection, TLS, ...
            raise ConnectorError(f"Couldn't reach {LABELS[c.kind]} ({type(exc).__name__}). "
                                 "Check the site address, or try again later.") from exc
    except ConnectorError as exc:
        c.status, c.last_error = "error", str(exc)[:2000]
        raise
    c.status, c.last_error = "ok", None
    return result


def test(session: Session, conn_id: uuid.UUID) -> Dict[str, Any]:
    c = _get(session, conn_id)
    message = _run(c, lambda m, cfg, sec: m.test(cfg, sec))
    session.flush()
    return {**_out(c), "message": message}


def pull_into_campaign(session: Session, campaign: Campaign, conn_id: uuid.UUID, actor: str,
                       application_name: Optional[str] = None) -> IngestResult:
    c = _get(session, conn_id)
    if not hasattr(KINDS[c.kind], "pull"):
        raise ConnectorError(f"{LABELS[c.kind]} connections can't pull access lists.")
    pulled = _run(c, lambda m, cfg, sec: m.pull(cfg, sec))
    df = pd.DataFrame(pulled.rows, columns=CANONICAL)
    mappings = [ColumnMapping(source="access_list", target=col, matched_column=col, confidence=1.0, method="exact")
                for col in CANONICAL]
    app_name = (application_name or "").strip() or pulled.application
    filename = f"{c.kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    result = load_access_frame(session, campaign, app_name, df, pulled.raw_bytes(), filename, mappings, actor,
                               source=c.kind)
    record_event(session, campaign, "source.pulled", {
        "snapshot_id": result.snapshot.id, "connection_id": c.id, "connection_name": c.name, "kind": c.kind,
        "config": c.config, "warnings": pulled.warnings}, actor)
    result.warnings = pulled.warnings + result.warnings
    return result


def verify_with_pull(session: Session, campaign: Campaign, application_id: uuid.UUID, conn_id: uuid.UUID,
                     actor: str) -> Dict[str, Any]:
    """Confirm remediations by pulling the application's current access from the same system it
    came from, instead of uploading an export. Same comparison and evidence as an upload."""
    c = _get(session, conn_id)
    remediation_service._owner(campaign, actor, "verify remediation")
    remediation_service._require_remediating(campaign)
    app = session.get(Application, application_id)
    if app is None:
        raise LookupError("application not found")
    came_from = session.scalar(select(SourceSnapshot.id).where(
        SourceSnapshot.campaign_id == campaign.id, SourceSnapshot.application_id == app.id,
        SourceSnapshot.kind == SourceKind.access_list, SourceSnapshot.source == c.kind))
    if came_from is None:
        # Account IDs differ between systems, so only the system that supplied the list can confirm it.
        raise ConnectorError(f"{app.name} wasn't pulled from {LABELS[c.kind]} in this campaign; "
                             "verify it with an uploaded access list instead.")
    pulled = _run(c, lambda m, cfg, sec: m.pull(cfg, sec))
    df = pd.DataFrame(pulled.rows, columns=CANONICAL)
    mappings = [ColumnMapping(source="verification", target=col, matched_column=col, confidence=1.0, method="exact")
                for col in CANONICAL]
    filename = f"{c.kind}-verify-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    result = remediation_service.verify_frame(session, campaign, app, df, pulled.raw_bytes(), filename, mappings,
                                              actor, source=c.kind)
    return {**result, "warnings": pulled.warnings}


def send_tickets(session: Session, campaign: Campaign, conn_id: uuid.UUID, actor: str) -> Dict[str, Any]:
    """Create a Jira ticket for every open remediation that has no ticket yet.

    Stops at the first failure; tickets already created are kept and recorded."""
    c = _get(session, conn_id)
    if c.kind != "jira_cloud":
        raise ConnectorError("Tickets can only be sent to a Jira Cloud connection.")
    remediation_service._owner(campaign, actor, "send remediation tickets")
    remediation_service._require_remediating(campaign)
    texts = remediation_service.ticket_texts(session, campaign)
    todo = [t for t in texts if not t["remediation"]["ticket_ref"]]
    created: List[Dict[str, Any]] = []
    error: Optional[str] = None
    for t in todo:
        r = t["remediation"]
        try:
            key = _run(c, lambda m, cfg, sec: m.create_ticket(
                cfg, sec, t["summary"], t["description"], labels=[f"risk-{r['severity'] or 'none'}"]))
        except ConnectorError as exc:
            error = str(exc)
            break
        remediation_service.update(session, campaign, r["id"], actor, ticket_ref=key)
        record_event(session, campaign, "remediation.ticket_sent",
                     {"remediation_id": r["id"], "ticket_ref": key, "connection_id": c.id,
                      "site": (c.config or {}).get("site")}, actor)
        created.append({"remediation_id": r["id"], "ticket_ref": key})
    session.flush()
    return {"created": created, "already_had_ticket": len(texts) - len(todo), "error": error}

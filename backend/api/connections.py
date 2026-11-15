"""Workspace connections (Google Workspace, Jira Cloud): add, list, test, remove.

Only workspace owners and admins can see or change connections. Secrets go in, never out.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.api.campaigns import Ctx, get_ctx
from backend.connectors.base import ConnectorError
from backend.connectors.secrets import SecretsUnavailable
from backend.domain import connections as connection_service
from backend.domain.workspaces import MANAGERS

router = APIRouter(prefix="/connections", tags=["connections"])
log = logging.getLogger("uar.connections")


class ConnectionIn(BaseModel):
    kind: str
    name: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    secret: Dict[str, Any] = Field(default_factory=dict)


def connector_failure(exc: ConnectorError) -> JSONResponse:
    """A connector error as a 422 that still lets the request's transaction commit, so the
    connection's error status is saved (raising HTTPException would roll it back)."""
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(exc)})


def secrets_unavailable(exc: SecretsUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


@router.get("")
def list_connections(ctx: Ctx = Depends(get_ctx)) -> List[Dict[str, Any]]:
    ctx.activate()
    ctx.need(MANAGERS, "see connections")
    return connection_service.list_connections(ctx.session)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connection(body: ConnectionIn, ctx: Ctx = Depends(get_ctx)):
    ctx.activate()
    ctx.need(MANAGERS, "add connections")
    try:
        out = connection_service.create(ctx.session, ctx.tenant_id, body.kind, body.name, body.config, body.secret,
                                        ctx.actor)
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except SecretsUnavailable as exc:
        raise secrets_unavailable(exc) from exc
    log.info("connection added", extra={"tenant": str(ctx.tenant_id), "kind": body.kind, "actor": ctx.actor})
    return out


@router.post("/{conn_id}/test")
def test_connection(conn_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    ctx.activate()
    ctx.need(MANAGERS, "test connections")
    try:
        return connection_service.test(ctx.session, conn_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConnectorError as exc:
        return connector_failure(exc)
    except SecretsUnavailable as exc:
        raise secrets_unavailable(exc) from exc


@router.delete("/{conn_id}")
def delete_connection(conn_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    ctx.activate()
    ctx.need(MANAGERS, "remove connections")
    try:
        connection_service.delete(ctx.session, conn_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    log.info("connection removed", extra={"tenant": str(ctx.tenant_id), "actor": ctx.actor})
    return {"ok": True}

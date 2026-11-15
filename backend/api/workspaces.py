"""Workspace API: list/create workspaces, manage members, accept invitations."""
from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import require_auth
from backend.db.session import get_engine
from backend.domain import workspaces as ws

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(pattern="^(owner|admin|reviewer|auditor)$")


class MemberUpdateIn(BaseModel):
    role: str = Field(pattern="^(owner|admin|reviewer|auditor)$")


def _engine():
    engine = get_engine()
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Workspaces need a database; DATABASE_URL is not set.")
    return engine


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ws.WorkspaceError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("")
def list_workspaces(principal: dict = Depends(require_auth)) -> Dict[str, List[Dict[str, Any]]]:
    return _call(lambda: ws.memberships(_engine(), principal))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workspace(body: WorkspaceIn, principal: dict = Depends(require_auth)):
    return _call(lambda: ws.create_workspace(_engine(), principal, body.name))


@router.patch("/{workspace_id}")
def rename_workspace(workspace_id: uuid.UUID, body: WorkspaceIn, principal: dict = Depends(require_auth)):
    _call(lambda: ws.rename(_engine(), principal, workspace_id, body.name))
    return {"ok": True}


@router.get("/{workspace_id}/members")
def list_members(workspace_id: uuid.UUID, principal: dict = Depends(require_auth)):
    return _call(lambda: ws.list_members(_engine(), principal, workspace_id))


@router.post("/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
def invite_member(workspace_id: uuid.UUID, body: InviteIn, principal: dict = Depends(require_auth)):
    return _call(lambda: ws.invite(_engine(), principal, workspace_id, body.email, body.role))


@router.patch("/{workspace_id}/members/{member_id}")
def change_member(workspace_id: uuid.UUID, member_id: uuid.UUID, body: MemberUpdateIn,
                  principal: dict = Depends(require_auth)):
    _call(lambda: ws.update_member(_engine(), principal, workspace_id, member_id, role=body.role))
    return {"ok": True}


@router.delete("/{workspace_id}/members/{member_id}")
def remove_member(workspace_id: uuid.UUID, member_id: uuid.UUID, principal: dict = Depends(require_auth)):
    _call(lambda: ws.update_member(_engine(), principal, workspace_id, member_id, remove=True))
    return {"ok": True}


@router.post("/{workspace_id}/invitation/{answer}")
def answer_invitation(workspace_id: uuid.UUID, answer: str, principal: dict = Depends(require_auth)):
    if answer not in ("accept", "decline"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "use accept or decline")
    _call(lambda: ws.respond(_engine(), principal, workspace_id, answer == "accept"))
    return {"ok": True}

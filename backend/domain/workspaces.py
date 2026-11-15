"""Shared workspaces: who may use which tenant, and as what.

A workspace is a tenant. People belong to workspaces through `workspace_member`
rows with a role:

    owner     everything, including inviting and removing members
    admin     runs campaigns (create, upload, analyze, sign off, remediate, close)
    reviewer  sees campaigns and decides the items assigned to them
    auditor   read-only, can download evidence packs

Everyone gets a personal workspace on first sign-in (as before). An owner invites
others by email; the invitee signs in with that email and accepts. One person can
belong to several workspaces (e.g. an MSP running reviews for many clients) and the
browser says which one each request is for with the X-Workspace-Id header.

Row-level security still isolates data per tenant underneath; this layer decides
which tenant a request may use at all.
"""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import Tenant, WorkspaceMember, WorkspaceRole

R = WorkspaceRole
MANAGERS = {R.owner, R.admin}
EVERYONE = {R.owner, R.admin, R.reviewer, R.auditor}

# The signed-in user's role in the workspace of the current request; set per request.
current_role: contextvars.ContextVar[Optional[WorkspaceRole]] = contextvars.ContextVar("current_role", default=None)


class WorkspaceError(ValueError):
    pass


class Forbidden(PermissionError):
    pass


@dataclass
class Membership:
    tenant_id: uuid.UUID
    name: str
    role: WorkspaceRole
    personal: bool


def _email(principal: Dict[str, Any]) -> Optional[str]:
    e = principal.get("email")
    return e.strip().lower() if isinstance(e, str) and "@" in e else None


def _sub(principal: Dict[str, Any]) -> str:
    sub = principal.get("sub")
    if not sub:
        raise Forbidden("token has no subject")
    return str(sub)


def _mine(principal: Dict[str, Any]):
    """SQL filter for membership rows that belong to this user.

    A row already tied to an Auth0 user matches only that user. A row not yet tied
    to anyone (an invitation) matches by verified email.
    """
    email = _email(principal)
    conds = [WorkspaceMember.user_sub == _sub(principal)]
    if email:
        conds.append(and_(WorkspaceMember.user_sub.is_(None), WorkspaceMember.email == email))
    return or_(*conds)


def _ensure_personal(session: Session, principal: Dict[str, Any]) -> None:
    """Create the user's personal workspace (and org workspace, if any) on first sign-in."""
    sub = _sub(principal)
    keys = [(f"user:{sub}", principal.get("email") or "My workspace", R.owner)]
    if principal.get("org_id"):
        keys.append((f"org:{principal['org_id']}", principal.get("org_name") or "Organization", R.admin))
    for key, name, role in keys:
        tenant = session.scalar(select(Tenant).where(Tenant.auth0_org_id == key))
        if tenant is None:
            tenant = Tenant(name=str(name), auth0_org_id=key, created_by=sub)
            session.add(tenant)
            try:
                session.flush()
            except IntegrityError:  # created concurrently
                session.rollback()
                tenant = session.scalar(select(Tenant).where(Tenant.auth0_org_id == key))
        member = session.scalar(select(WorkspaceMember).where(
            WorkspaceMember.tenant_id == tenant.id, WorkspaceMember.user_sub == sub))
        if member is None:
            session.add(WorkspaceMember(tenant_id=tenant.id, user_sub=sub, email=_email(principal), role=role,
                                        status="active", invited_by="self", accepted_at=datetime.now(timezone.utc)))
            try:
                session.flush()
            except IntegrityError:
                session.rollback()


def _link_identity(session: Session, principal: Dict[str, Any]) -> None:
    """Fill in sub on rows found by email, and email on rows found by sub (accepted ones only)."""
    sub, email = _sub(principal), _email(principal)
    for m in session.scalars(select(WorkspaceMember).where(_mine(principal))):
        if m.status != "active":
            continue
        if m.user_sub is None:
            m.user_sub = sub
        if m.email is None and email:
            m.email = email


def memberships(engine: Engine, principal: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """The user's active workspaces and pending invitations."""
    with Session(engine) as session:
        _ensure_personal(session, principal)
        _link_identity(session, principal)
        session.commit()
        rows = session.execute(
            select(WorkspaceMember, Tenant).join(Tenant, Tenant.id == WorkspaceMember.tenant_id)
            .where(_mine(principal)).order_by(Tenant.created_at)
        ).all()
        sub = _sub(principal)
        active, invites = [], []
        for m, t in rows:
            item = {"id": t.id, "name": t.name, "role": m.role.value,
                    "personal": t.auth0_org_id == f"user:{sub}", "invited_by": m.invited_by}
            (active if m.status == "active" else invites).append(item)
        return {"workspaces": active, "invitations": invites}


def resolve(engine: Engine, principal: Dict[str, Any], workspace_id: Optional[str]) -> Membership:
    """Which workspace this request is for, and the user's role there.

    With no X-Workspace-Id the user's personal workspace is used, which keeps older
    clients working. An id the user isn't an active member of is refused.
    """
    with Session(engine) as session:
        _ensure_personal(session, principal)
        _link_identity(session, principal)
        session.commit()
        q = (select(WorkspaceMember, Tenant).join(Tenant, Tenant.id == WorkspaceMember.tenant_id)
             .where(_mine(principal), WorkspaceMember.status == "active"))
        if workspace_id:
            try:
                wid = uuid.UUID(str(workspace_id))
            except ValueError as exc:
                raise Forbidden("invalid workspace id") from exc
            row = session.execute(q.where(Tenant.id == wid)).first()
            if row is None:
                raise Forbidden("you are not a member of this workspace")
        else:
            row = session.execute(q.where(Tenant.auth0_org_id == f"user:{_sub(principal)}")).first()
        m, t = row
        return Membership(t.id, t.name, m.role, t.auth0_org_id == f"user:{_sub(principal)}")


def require(role: Optional[WorkspaceRole], allowed: set, what: str) -> None:
    if role not in allowed:
        raise Forbidden(f"your role ({role.value if role else 'none'}) can't {what}")


# ---------------------------------------------------------------------------
# Managing workspaces and members
# ---------------------------------------------------------------------------


def create_workspace(engine: Engine, principal: Dict[str, Any], name: str) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("give the workspace a name")
    with Session(engine) as session:
        t = Tenant(name=name[:255], created_by=_sub(principal))
        session.add(t)
        session.flush()
        session.add(WorkspaceMember(tenant_id=t.id, user_sub=_sub(principal), email=_email(principal), role=R.owner,
                                    status="active", invited_by="self", accepted_at=datetime.now(timezone.utc)))
        session.commit()
        return {"id": t.id, "name": t.name, "role": "owner", "personal": False, "invited_by": "self"}


def _member_of(session: Session, principal: Dict[str, Any], tenant_id: uuid.UUID) -> WorkspaceMember:
    m = session.scalar(select(WorkspaceMember).where(
        WorkspaceMember.tenant_id == tenant_id, WorkspaceMember.status == "active", _mine(principal)))
    if m is None:
        raise Forbidden("you are not a member of this workspace")
    return m


def list_members(engine: Engine, principal: Dict[str, Any], tenant_id: uuid.UUID) -> List[Dict[str, Any]]:
    with Session(engine) as session:
        _member_of(session, principal, tenant_id)
        rows = session.scalars(select(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id)
                               .order_by(WorkspaceMember.created_at))
        return [{"id": m.id, "email": m.email, "role": m.role.value, "status": m.status,
                 "invited_by": m.invited_by, "accepted_at": m.accepted_at,
                 "is_you": m.user_sub == principal.get("sub")} for m in rows]


def invite(engine: Engine, principal: Dict[str, Any], tenant_id: uuid.UUID, email: str, role: str) -> Dict[str, Any]:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 320:
        raise WorkspaceError("enter a valid email address")
    try:
        r = WorkspaceRole(role)
    except ValueError as exc:
        raise WorkspaceError("role must be owner, admin, reviewer or auditor") from exc
    with Session(engine) as session:
        me = _member_of(session, principal, tenant_id)
        require(me.role, {R.owner}, "invite members")
        existing = session.scalar(select(WorkspaceMember).where(
            WorkspaceMember.tenant_id == tenant_id, WorkspaceMember.email == email))
        if existing is not None:
            raise WorkspaceError(f"{email} is already {'a member' if existing.status == 'active' else 'invited'}")
        m = WorkspaceMember(tenant_id=tenant_id, email=email, role=r, status="invited",
                            invited_by=_email(principal) or _sub(principal))
        session.add(m)
        session.commit()
        return {"id": m.id, "email": email, "role": r.value, "status": "invited"}


def respond(engine: Engine, principal: Dict[str, Any], tenant_id: uuid.UUID, accept: bool) -> None:
    email = _email(principal)
    if not email:
        raise Forbidden("your sign-in has no verified email, so invitations can't be matched to you")
    with Session(engine) as session:
        m = session.scalar(select(WorkspaceMember).where(
            WorkspaceMember.tenant_id == tenant_id, WorkspaceMember.email == email,
            WorkspaceMember.status == "invited"))
        if m is None:
            raise LookupError("invitation not found")
        if accept:
            if session.scalar(select(WorkspaceMember.id).where(
                    WorkspaceMember.tenant_id == tenant_id, WorkspaceMember.user_sub == _sub(principal))):
                raise WorkspaceError("you are already a member of this workspace")
            m.status, m.user_sub, m.accepted_at = "active", _sub(principal), datetime.now(timezone.utc)
        else:
            session.delete(m)
        session.commit()


def update_member(engine: Engine, principal: Dict[str, Any], tenant_id: uuid.UUID, member_id: uuid.UUID,
                  role: Optional[str] = None, remove: bool = False) -> None:
    with Session(engine) as session:
        me = _member_of(session, principal, tenant_id)
        require(me.role, {R.owner}, "change members")
        m = session.scalar(select(WorkspaceMember).where(
            WorkspaceMember.id == member_id, WorkspaceMember.tenant_id == tenant_id))
        if m is None:
            raise LookupError("member not found")
        owners = list(session.scalars(select(WorkspaceMember).where(
            WorkspaceMember.tenant_id == tenant_id, WorkspaceMember.role == R.owner,
            WorkspaceMember.status == "active")))
        losing_owner = m.role == R.owner and m.status == "active" and (remove or (role and role != "owner"))
        if losing_owner and len(owners) <= 1:
            raise WorkspaceError("a workspace needs at least one owner")
        if remove:
            session.delete(m)
        elif role:
            try:
                m.role = WorkspaceRole(role)
            except ValueError as exc:
                raise WorkspaceError("role must be owner, admin, reviewer or auditor") from exc
        session.commit()


def rename(engine: Engine, principal: Dict[str, Any], tenant_id: uuid.UUID, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("give the workspace a name")
    with Session(engine) as session:
        me = _member_of(session, principal, tenant_id)
        require(me.role, {R.owner}, "rename the workspace")
        t = session.get(Tenant, tenant_id)
        t.name = name[:255]
        session.commit()

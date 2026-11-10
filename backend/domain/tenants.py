"""Map an authenticated principal to a tenant.

Users in an Auth0 Organization share that organization's tenant (the token's
org_id claim). Users outside any organization get a personal tenant keyed by
their subject, so nobody ever lands in another customer's data.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import Tenant


def tenant_key(principal: Dict[str, Any]) -> str:
    org = principal.get("org_id")
    if org:
        return f"org:{org}"
    sub = principal.get("sub")
    if not sub:
        raise PermissionError("token has no subject")
    return f"user:{sub}"


def resolve_tenant(engine: Engine, principal: Dict[str, Any]) -> uuid.UUID:
    key = tenant_key(principal)
    with Session(engine) as session:
        tenant_id = session.scalar(select(Tenant.id).where(Tenant.auth0_org_id == key))
        if tenant_id:
            return tenant_id
        name = principal.get("org_name") or principal.get("email") or key
        tenant = Tenant(name=str(name), auth0_org_id=key)
        session.add(tenant)
        try:
            session.commit()
            return tenant.id
        except IntegrityError:  # created concurrently by another request
            session.rollback()
            return session.scalar(select(Tenant.id).where(Tenant.auth0_org_id == key))

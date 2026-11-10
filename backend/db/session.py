from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> Optional[str]:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return None
    # Render and Heroku hand out postgres:// URLs; SQLAlchemy wants an explicit driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache(maxsize=4)
def _engine_for(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def get_engine() -> Optional[Engine]:
    url = database_url()
    return _engine_for(url) if url else None


@contextmanager
def tenant_session(tenant_id: uuid.UUID, engine: Optional[Engine] = None) -> Iterator[Session]:
    """A transaction in which Postgres row-level security sees only `tenant_id`.

    `SET LOCAL` scopes the setting to this transaction, so a pooled connection
    never carries one tenant's id into another request.
    """
    engine = engine or get_engine()
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured")
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        with session.begin():
            session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
            yield session

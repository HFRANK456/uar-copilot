from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from backend.db.models import Base
from backend.db.session import database_url

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL must be set to run migrations")
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

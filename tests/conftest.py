from pathlib import Path

import pandas as pd
import pytest

DATA = Path(__file__).resolve().parents[1] / "data"
AS_OF = pd.Timestamp("2026-11-10")


@pytest.fixture
def data_dir() -> Path:
    return DATA


def load(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / name)


# ---------------------------------------------------------------------------
# Database fixtures (Phase 1). Tests marked `db` need TEST_DATABASE_URL pointing
# at an empty Postgres database owned by a NON-superuser role, because
# superusers bypass row-level security.
# ---------------------------------------------------------------------------

import os  # noqa: E402

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def db_engine():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    from backend.db.session import database_url

    engine = create_engine(database_url())
    with engine.begin() as conn:
        is_super = conn.execute(text("select rolsuper from pg_roles where rolname = current_user")).scalar()
        if is_super:
            pytest.fail("TEST_DATABASE_URL must use a non-superuser role (superusers bypass RLS)")
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(DATA.parent / "backend" / "alembic.ini"))
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def make_tenant(db_engine):
    import uuid as _uuid

    from sqlalchemy.orm import Session

    from backend.db.models import Tenant

    def _make(name="t"):
        with Session(db_engine) as s:
            t = Tenant(name=name, auth0_org_id=f"test:{_uuid.uuid4()}")
            s.add(t)
            s.commit()
            return t.id

    return _make

"""Ingest access lists and HR rosters into a campaign (FR-02, FR-03, FR-05).

Every file is hashed and stored as an immutable snapshot before anything is
parsed out of it, and the column-mapping decisions are kept on the snapshot.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import (
    Account,
    AccountEntitlement,
    AccountType,
    Application,
    Campaign,
    CampaignState,
    Entitlement,
    Person,
    SourceKind,
    SourceSnapshot,
)
from backend.domain.campaigns import InvalidTransition, transition
from backend.domain.evidence import record_event
from backend.mapping import ACCESS_LIST_ALIASES, HR_ROSTER_ALIASES, map_columns
from backend.privileged_access_catalog import classify_privileged_access
from backend.rules import _normalize_status_value
from schemas.user_schema import ColumnMapping

MAX_FILE_BYTES = 25 * 1024 * 1024
INGESTIBLE_STATES = {
    CampaignState.draft,
    CampaignState.ingesting,
    CampaignState.resolving_identities,
    CampaignState.analyzing,
}

_SERVICE_PATTERN = re.compile(r"(^|[._\-@])(svc|service|bot|api|system|noreply|no-reply|daemon|automation|integration)([._\-@0-9]|$)")
_SHARED_PATTERN = re.compile(r"^(admin|shared|team|support|ops|info|it|helpdesk|root)([._\-@0-9]|$)")


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    snapshot: SourceSnapshot
    mappings: List[ColumnMapping]
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _read_table(raw: bytes, filename: Optional[str]) -> pd.DataFrame:
    name = (filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            return pd.read_excel(io.BytesIO(raw), dtype=str)
        return pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, na_values=[""])
    except Exception as exc:
        raise IngestError(f"Could not parse {filename or 'file'} as CSV or Excel") from exc


def _clean(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> Optional[date]:
    text = _clean(value)
    if not text:
        return None
    ts = pd.to_datetime(text, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def _datetime(value: Any) -> Optional[datetime]:
    text = _clean(value)
    if not text:
        return None
    ts = pd.to_datetime(text, errors="coerce", utc=True)
    return None if pd.isna(ts) else ts.to_pydatetime()


def _email(value: Any) -> Optional[str]:
    text = _clean(value)
    return text.lower() if text and "@" in text else None


def classify_account_type(native_id: str, username: Optional[str], email: Optional[str]) -> AccountType:
    for candidate in (username, native_id, (email or "").split("@")[0]):
        if not candidate:
            continue
        c = candidate.lower()
        if _SERVICE_PATTERN.search(c):
            return AccountType.service
        if _SHARED_PATTERN.search(c):
            return AccountType.shared
    return AccountType.unknown  # human vs external is settled by identity resolution


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def _snapshot(
    session: Session,
    campaign: Campaign,
    kind: SourceKind,
    raw: bytes,
    filename: Optional[str],
    actor: str,
    df: pd.DataFrame,
    mappings: List[ColumnMapping],
    application: Optional[Application],
) -> SourceSnapshot:
    snap = SourceSnapshot(
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        kind=kind,
        application_id=application.id if application else None,
        source="upload",
        filename=filename,
        content=raw,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        row_count=len(df),
        column_mapping=[m.model_dump() for m in mappings],
        created_by=actor,
    )
    session.add(snap)
    session.flush()
    return snap


def _prepare_campaign(session: Session, campaign: Campaign, actor: str) -> None:
    if campaign.state not in INGESTIBLE_STATES:
        raise InvalidTransition(f"cannot ingest into a campaign in state {campaign.state.value}")
    # From later states, new data invalidates identity resolution and analysis.
    transition(session, campaign, CampaignState.ingesting, actor)


def _check_size(raw: bytes, filename: Optional[str]) -> None:
    if len(raw) == 0:
        raise IngestError(f"{filename or 'file'} is empty")
    if len(raw) > MAX_FILE_BYTES:
        raise IngestError(f"{filename or 'file'} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")


# ---------------------------------------------------------------------------
# HR roster
# ---------------------------------------------------------------------------


def ingest_hr_roster(
    session: Session, campaign: Campaign, raw: bytes, filename: Optional[str], actor: str
) -> IngestResult:
    _check_size(raw, filename)
    mappings: List[ColumnMapping] = []
    df = map_columns(_read_table(raw, filename), HR_ROSTER_ALIASES, "hr_roster", mappings)
    if "employee_id" not in df.columns and "email" not in df.columns:
        raise IngestError("HR roster needs an employee ID or email column")

    _prepare_campaign(session, campaign, actor)
    snap = _snapshot(session, campaign, SourceKind.hr_roster, raw, filename, actor, df, mappings, None)

    created = updated = skipped = 0
    warnings: List[str] = []
    by_key: Dict[str, Person] = {}
    for idx, row in df.iterrows():
        employee_id = _clean(row.get("employee_id"))
        email = _email(row.get("email"))
        if not employee_id and not email:
            skipped += 1
            warnings.append(f"row {idx + 2}: no employee ID or email; skipped")
            continue

        person = None
        if employee_id:
            person = session.scalar(select(Person).where(Person.employee_id == employee_id))
        if person is None and email:
            person = session.scalar(select(Person).where(Person.email == email))
        if person is None:
            person = Person(tenant_id=campaign.tenant_id, created_by=actor)
            session.add(person)
            created += 1
        else:
            updated += 1

        person.employee_id = employee_id or person.employee_id
        person.email = email or person.email
        person.name = _clean(row.get("name")) or person.name
        person.manager_ref = _clean(row.get("manager"))
        person.department = _clean(row.get("department"))
        person.title = _clean(row.get("title"))
        status = _clean(row.get("status"))
        person.employment_status = status.lower() if status else None
        person.hire_date = _date(row.get("hire_date"))
        person.termination_date = _date(row.get("termination_date"))
        person.source_snapshot_id = snap.id
        session.flush()
        for key in (employee_id, email):
            if key:
                by_key[key.lower()] = person

    # Second pass: resolve manager references now that everyone is loaded.
    unresolved_managers = 0
    for person in set(by_key.values()):
        ref = (person.manager_ref or "").strip().lower()
        if not ref:
            person.manager_id = None
            continue
        manager = by_key.get(ref)
        if manager is None:
            manager = session.scalar(select(Person).where((Person.employee_id == person.manager_ref) | (Person.email == ref)))
        person.manager_id = manager.id if manager and manager.id != person.id else None
        if person.manager_id is None:
            unresolved_managers += 1
    session.flush()

    counts = {
        "rows": len(df),
        "people_created": created,
        "people_updated": updated,
        "rows_skipped": skipped,
        "managers_unresolved": unresolved_managers,
    }
    _record_ingest(session, campaign, snap, mappings, counts, actor)
    return IngestResult(snap, mappings, counts, warnings[:50])


# ---------------------------------------------------------------------------
# Access list
# ---------------------------------------------------------------------------


def _get_or_create_application(session: Session, campaign: Campaign, name: str, actor: str) -> Application:
    app = session.scalar(select(Application).where(Application.name == name))
    if app is None:
        app = Application(tenant_id=campaign.tenant_id, name=name, created_by=actor)
        session.add(app)
        session.flush()
    return app


def _get_or_create_entitlement(
    session: Session, campaign: Campaign, app: Application, name: str, actor: str, cache: Dict[str, Entitlement]
) -> Entitlement:
    key = name.lower()
    if key in cache:
        return cache[key]
    ent = session.scalar(select(Entitlement).where(Entitlement.application_id == app.id, Entitlement.name == name))
    if ent is None:
        match = classify_privileged_access(name)
        ent = Entitlement(
            tenant_id=campaign.tenant_id,
            application_id=app.id,
            name=name,
            privilege_tier=match.risk_tier if match.is_privileged else ("low" if "admin" in key else "none"),
            category=match.privilege_category if match.is_privileged else ("unclassified_admin" if "admin" in key else "standard_access"),
            created_by=actor,
        )
        session.add(ent)
        session.flush()
    cache[key] = ent
    return ent


def ingest_access_list(
    session: Session,
    campaign: Campaign,
    application_name: str,
    raw: bytes,
    filename: Optional[str],
    actor: str,
) -> IngestResult:
    application_name = (application_name or "").strip()
    if not application_name:
        raise IngestError("application name is required for an access list")
    _check_size(raw, filename)
    mappings: List[ColumnMapping] = []
    df = map_columns(_read_table(raw, filename), ACCESS_LIST_ALIASES, "access_list", mappings)
    if "native_id" not in df.columns:
        if "username" in df.columns:
            df["native_id"] = df["username"]
        elif "email" in df.columns:
            df["native_id"] = df["email"]
        else:
            raise IngestError("access list needs a user ID, username or email column")

    _prepare_campaign(session, campaign, actor)
    app = _get_or_create_application(session, campaign, application_name, actor)
    snap = _snapshot(session, campaign, SourceKind.access_list, raw, filename, actor, df, mappings, app)

    accounts: Dict[str, Account] = {}
    ent_cache: Dict[str, Entitlement] = {}
    grants = skipped = 0
    warnings: List[str] = []
    for idx, row in df.iterrows():
        native_id = _clean(row.get("native_id"))
        if not native_id:
            skipped += 1
            warnings.append(f"row {idx + 2}: no account identifier; skipped")
            continue
        account = accounts.get(native_id)
        if account is None:
            username = _clean(row.get("username"))
            email = _email(row.get("email")) or _email(native_id) or _email(username)
            status = _clean(row.get("status"))
            account = Account(
                tenant_id=campaign.tenant_id,
                application_id=app.id,
                source_snapshot_id=snap.id,
                native_id=native_id,
                username=username,
                email=email,
                display_name=_clean(row.get("display_name")),
                status=_normalize_status_value(status) if status is not None else None,
                last_login_at=_datetime(row.get("last_login")),
                account_type=classify_account_type(native_id, username, email),
                row_ref=int(idx),
                created_by=actor,
            )
            session.add(account)
            session.flush()
            accounts[native_id] = account

        role = _clean(row.get("role"))
        if role:
            ent = _get_or_create_entitlement(session, campaign, app, role, actor, ent_cache)
            exists = session.scalar(
                select(AccountEntitlement.id).where(
                    AccountEntitlement.account_id == account.id, AccountEntitlement.entitlement_id == ent.id
                )
            )
            if not exists:
                session.add(
                    AccountEntitlement(
                        tenant_id=campaign.tenant_id, account_id=account.id, entitlement_id=ent.id, created_by=actor
                    )
                )
                grants += 1
    session.flush()

    counts = {
        "rows": len(df),
        "accounts": len(accounts),
        "entitlement_grants": grants,
        "entitlements": len(ent_cache),
        "rows_skipped": skipped,
        "service_or_shared_accounts": sum(
            1 for a in accounts.values() if a.account_type in (AccountType.service, AccountType.shared)
        ),
    }
    _record_ingest(session, campaign, snap, mappings, counts, actor, application=app.name)
    return IngestResult(snap, mappings, counts, warnings[:50])


def _record_ingest(
    session: Session,
    campaign: Campaign,
    snap: SourceSnapshot,
    mappings: List[ColumnMapping],
    counts: Dict[str, int],
    actor: str,
    application: Optional[str] = None,
) -> None:
    record_event(
        session,
        campaign,
        "source.ingested",
        {
            "snapshot_id": snap.id,
            "kind": snap.kind.value,
            "application": application,
            "filename": snap.filename,
            "sha256": snap.sha256,
            "size_bytes": snap.size_bytes,
            "row_count": snap.row_count,
            "column_mapping": [m.model_dump() for m in mappings],
            "counts": counts,
        },
        actor,
    )


def confirm_mapping(session: Session, campaign: Campaign, snapshot_id: uuid.UUID, actor: str) -> SourceSnapshot:
    snap = session.scalar(
        select(SourceSnapshot).where(SourceSnapshot.id == snapshot_id, SourceSnapshot.campaign_id == campaign.id)
    )
    if snap is None:
        raise LookupError(f"snapshot {snapshot_id} not found")
    if snap.mapping_confirmed_by is None:
        snap.mapping_confirmed_by = actor
        record_event(
            session,
            campaign,
            "source.mapping_confirmed",
            {"snapshot_id": snap.id, "confirmed_at": datetime.now(timezone.utc)},
            actor,
        )
        session.flush()
    return snap


def snapshot_needs_confirmation(snap: SourceSnapshot) -> bool:
    return snap.mapping_confirmed_by is None and any(m.get("needs_confirmation") for m in snap.column_mapping or [])

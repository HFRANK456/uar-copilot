"""Identity resolution (FR-07, FR-08): link each app account to a person in the HR roster.

Signals, strongest first:
    employee_id  account ID or username equals the person's employee ID     1.00
    email        account email equals the person's email                    0.99
    username     account username/ID equals the local part of their email   0.92
    name         display name equals the person's name                      0.80
    name_pattern username looks like first.last / flast / firstl / ...      0.75

At or above AUTO_LINK_THRESHOLD a link is made automatically. Below it, or
when two people tie for the best score, the account is queued as ambiguous
for a human. Service and shared accounts are set aside as non-human; accounts
on an email domain the company doesn't use are marked external. Links a human
confirmed are never overwritten by a re-run.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import (
    Account,
    AccountType,
    Campaign,
    CampaignState,
    IdentityLink,
    Person,
    SourceKind,
    SourceSnapshot,
)
from backend.domain.campaigns import InvalidTransition, transition
from backend.domain.evidence import record_event

AUTO_LINK_THRESHOLD = 0.9
CANDIDATE_FLOOR = 0.7
MAX_CANDIDATES = 5

SCORES = {"employee_id": 1.0, "email": 0.99, "username": 0.92, "name": 0.8, "name_pattern": 0.75}

RESOLVED_STATUSES = {"matched", "non_human", "external"}
OPEN_STATUSES = {"ambiguous", "unmatched"}


@dataclass(frozen=True)
class Candidate:
    person_id: uuid.UUID
    method: str
    confidence: float

    def as_json(self) -> dict:
        return {"person_id": str(self.person_id), "method": self.method, "confidence": self.confidence}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def _name_tokens(name: Optional[str]) -> List[str]:
    if not name:
        return []
    name = _fold(name)
    if "," in name:  # "Shah, Priya" -> "priya shah"
        last, _, first = name.partition(",")
        name = f"{first} {last}"
    return [t for t in re.split(r"[^a-z]+", name) if t]


def _local(value: Optional[str]) -> Optional[str]:
    """'Priya.Shah@acme.io' -> 'priya.shah'; 'ACME\\pshah' -> 'pshah'."""
    if not value:
        return None
    v = _fold(value)
    v = v.split("\\")[-1]
    v = v.split("@")[0]
    return v or None


def _squash(value: str) -> str:
    return re.sub(r"[^a-z]", "", value)


def username_patterns(tokens: List[str]) -> Set[str]:
    """Usernames a person with these name tokens commonly gets."""
    if len(tokens) < 2:
        return set()
    first, last = tokens[0], tokens[-1]
    return {
        first + last,          # priyashah  (also covers priya.shah / priya_shah after squashing)
        last + first,          # shahpriya
        first[0] + last,       # pshah
        first + last[0],       # priyas
        last + first[0],       # shahp
    }


def _domain(email: Optional[str]) -> Optional[str]:
    return email.split("@", 1)[1].lower() if email and "@" in email else None


# ---------------------------------------------------------------------------
# Index over people
# ---------------------------------------------------------------------------


class PersonIndex:
    def __init__(self, people: Iterable[Person]):
        self.by_employee_id: Dict[str, Set[uuid.UUID]] = defaultdict(set)
        self.by_email: Dict[str, Set[uuid.UUID]] = defaultdict(set)
        self.by_email_local: Dict[str, Set[uuid.UUID]] = defaultdict(set)
        self.by_name: Dict[str, Set[uuid.UUID]] = defaultdict(set)
        self.by_pattern: Dict[str, Set[uuid.UUID]] = defaultdict(set)
        self.domains: Set[str] = set()
        for p in people:
            if p.employee_id:
                self.by_employee_id[_fold(p.employee_id)].add(p.id)
            if p.email:
                email = _fold(p.email)
                self.by_email[email].add(p.id)
                local = _local(email)
                if local:
                    self.by_email_local[local].add(p.id)
                    self.by_email_local[_squash(local)].add(p.id)
                if _domain(email):
                    self.domains.add(_domain(email))
            tokens = _name_tokens(p.name)
            if tokens:
                self.by_name[" ".join(tokens)].add(p.id)
                for pattern in username_patterns(tokens):
                    self.by_pattern[pattern].add(p.id)

    def candidates(self, account: Account) -> List[Candidate]:
        best: Dict[uuid.UUID, Candidate] = {}

        def offer(ids: Iterable[uuid.UUID], method: str) -> None:
            for pid in ids:
                c = Candidate(pid, method, SCORES[method])
                if pid not in best or c.confidence > best[pid].confidence:
                    best[pid] = c

        ids = [v for v in (account.native_id, account.username) if v]
        for v in ids:
            offer(self.by_employee_id.get(_fold(v), ()), "employee_id")
        if account.email:
            offer(self.by_email.get(_fold(account.email), ()), "email")
        for v in ids:
            local = _local(v)
            if local:
                offer(self.by_email_local.get(local, ()), "username")
                offer(self.by_email_local.get(_squash(local), ()), "username")
        tokens = _name_tokens(account.display_name)
        if tokens:
            offer(self.by_name.get(" ".join(tokens), ()), "name")
        for v in [*ids, account.email]:
            local = _local(v)
            if local:
                offer(self.by_pattern.get(_squash(local), ()), "name_pattern")

        ranked = sorted(best.values(), key=lambda c: (-c.confidence, str(c.person_id)))
        return [c for c in ranked if c.confidence >= CANDIDATE_FLOOR][:MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def campaign_accounts(session: Session, campaign: Campaign) -> List[Account]:
    snapshot_ids = select(SourceSnapshot.id).where(
        SourceSnapshot.campaign_id == campaign.id, SourceSnapshot.kind == SourceKind.access_list
    )
    return list(session.scalars(select(Account).where(Account.source_snapshot_id.in_(snapshot_ids))))


def classify(account: Account, index: PersonIndex) -> Tuple[str, Optional[Candidate], List[Candidate]]:
    """Return (status, chosen candidate or None, candidates)."""
    if account.account_type in (AccountType.service, AccountType.shared):
        return "non_human", None, []
    cands = index.candidates(account)
    if cands:
        top = cands[0]
        tied = len(cands) > 1 and cands[1].confidence == top.confidence
        if top.confidence >= AUTO_LINK_THRESHOLD and not tied:
            return "matched", top, cands
        return "ambiguous", None, cands
    domain = _domain(account.email)
    if domain and index.domains and domain not in index.domains:
        return "external", None, []
    return "unmatched", None, []


def resolve_identities(session: Session, campaign: Campaign, actor: str, actor_kind: str = "human") -> Dict[str, int]:
    if campaign.state not in (CampaignState.ingesting, CampaignState.resolving_identities):
        raise InvalidTransition(f"cannot resolve identities in state {campaign.state.value}")

    people = list(session.scalars(select(Person)))
    if not people:
        raise ValueError("upload an HR roster before resolving identities")
    accounts = campaign_accounts(session, campaign)
    if not accounts:
        raise ValueError("upload at least one access list before resolving identities")

    index = PersonIndex(people)
    existing = {
        link.account_id: link
        for link in session.scalars(select(IdentityLink).where(IdentityLink.account_id.in_([a.id for a in accounts])))
    }

    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0, "non_human": 0, "external": 0, "kept_manual": 0}
    by_method: Dict[str, int] = defaultdict(int)
    for account in accounts:
        link = existing.get(account.id)
        if link is not None and link.method == "manual":
            counts["kept_manual"] += 1
            continue

        status, chosen, cands = classify(account, index)
        if link is None:
            link = IdentityLink(tenant_id=campaign.tenant_id, account_id=account.id, created_by=actor)
            session.add(link)
        link.status = status
        link.person_id = chosen.person_id if chosen else None
        link.method = chosen.method if chosen else "none"
        link.confidence = chosen.confidence if chosen else (cands[0].confidence if cands else 0.0)
        link.candidates = [c.as_json() for c in cands]
        link.confirmed_by = "system" if chosen else None
        link.confirmed_at = datetime.now(timezone.utc) if chosen else None

        if status == "external":
            account.account_type = AccountType.external
        elif status == "matched":
            account.account_type = AccountType.human
        counts[status] += 1
        if chosen:
            by_method[chosen.method] += 1
    session.flush()

    if campaign.state == CampaignState.ingesting:
        transition(session, campaign, CampaignState.resolving_identities, actor, actor_kind)
    summary = {**counts, "accounts": len(accounts), "people": len(people)}
    record_event(
        session,
        campaign,
        "identities.resolved",
        {
            **summary,
            "auto_link_threshold": AUTO_LINK_THRESHOLD,
            "matched_by_method": dict(by_method),
        },
        actor,
        actor_kind,
    )
    return summary


def confirm_link(
    session: Session,
    campaign: Campaign,
    account_id: uuid.UUID,
    actor: str,
    person_id: Optional[uuid.UUID] = None,
    account_type: Optional[AccountType] = None,
) -> IdentityLink:
    """A human links an account to a person, or marks it as not a person.

    Exactly one of person_id (link to that person) or account_type
    (service/shared/external: not an employee) must be given.
    """
    if campaign.state != CampaignState.resolving_identities:
        raise InvalidTransition(f"cannot change identity links in state {campaign.state.value}")
    if (person_id is None) == (account_type is None):
        raise ValueError("give either person_id or account_type")
    if account_type is not None and account_type not in (AccountType.service, AccountType.shared, AccountType.external):
        raise ValueError("account_type must be service, shared or external")

    account = next((a for a in campaign_accounts(session, campaign) if a.id == account_id), None)
    if account is None:
        raise LookupError("account not found in this campaign")
    if person_id is not None and session.scalar(select(Person.id).where(Person.id == person_id)) is None:
        raise LookupError("person not found")

    link = session.scalar(select(IdentityLink).where(IdentityLink.account_id == account_id))
    previous = {"status": link.status, "person_id": link.person_id} if link else None
    if link is None:
        link = IdentityLink(tenant_id=campaign.tenant_id, account_id=account_id, created_by=actor, candidates=[])
        session.add(link)

    link.method = "manual"
    link.confidence = 1.0
    link.confirmed_by = actor
    link.confirmed_at = datetime.now(timezone.utc)
    if person_id is not None:
        link.person_id = person_id
        link.status = "matched"
        account.account_type = AccountType.human
    else:
        link.person_id = None
        link.status = "external" if account_type == AccountType.external else "non_human"
        account.account_type = account_type
    session.flush()

    record_event(
        session,
        campaign,
        "identity.confirmed",
        {
            "account_id": account_id,
            "native_id": account.native_id,
            "person_id": person_id,
            "account_type": account.account_type.value,
            "previous": previous,
        },
        actor,
    )
    return link


def identity_summary(session: Session, campaign: Campaign) -> Dict[str, object]:
    accounts = campaign_accounts(session, campaign)
    links = {
        link.account_id: link
        for link in session.scalars(select(IdentityLink).where(IdentityLink.account_id.in_([a.id for a in accounts])))
    }
    counts: Dict[str, int] = defaultdict(int)
    for a in accounts:
        counts[links[a.id].status if a.id in links else "not_resolved"] += 1
    total = len(accounts)
    auto = sum(1 for link in links.values() if link.status == "matched" and link.method != "manual")
    humans = sum(1 for link in links.values() if link.status in ("matched", "ambiguous", "unmatched"))
    return {
        "accounts": total,
        **dict(counts),
        "auto_match_rate": round(auto / humans, 3) if humans else None,
        "open": sum(counts[s] for s in (*OPEN_STATUSES, "not_resolved")),
        "ready_for_analysis": total > 0 and all(counts[s] == 0 for s in (*OPEN_STATUSES, "not_resolved")),
    }

"""Identity matching: pure unit tests over the matcher, no database."""
import uuid

import pytest

from backend.db.models import Account, AccountType, Person
from backend.domain.identity import PersonIndex, classify, username_patterns


def person(name, email=None, employee_id=None):
    return Person(id=uuid.uuid4(), name=name, email=email, employee_id=employee_id)


def account(native_id, email=None, username=None, display_name=None, account_type=AccountType.unknown):
    return Account(native_id=native_id, email=email, username=username, display_name=display_name, account_type=account_type)


PRIYA = person("Priya Shah", "priya.shah@acme.io", "E101")
MARCUS = person("Marcus Lee", "marcus.lee@acme.io", "E102")
SAM = person("Sam Ortiz", "sam.ortiz@acme.io", "E104")
JORDAN_K = person("Jordan Kim", "jordan.kim@acme.io", "E103")
JULIA_K = person("Julia Kim", "julia.kim@acme.io", "E106")
ZOE = person("Zoë Ångström", "zoe.angstrom@acme.io", "E107")
INDEX = PersonIndex([PRIYA, MARCUS, SAM, JORDAN_K, JULIA_K, ZOE])


@pytest.mark.parametrize(
    "acct,status,who,method",
    [
        (account("E104"), "matched", SAM, "employee_id"),
        (account("x1", email="Priya.Shah@ACME.io"), "matched", PRIYA, "email"),
        (account("priya.shah"), "matched", PRIYA, "username"),
        (account("ACME\\marcus.lee"), "matched", MARCUS, "username"),
        (account("marcus_lee"), "matched", MARCUS, "username"),
        (account("zoe.angstrom"), "matched", ZOE, "username"),
    ],
)
def test_strong_signals_auto_link(acct, status, who, method):
    got_status, chosen, _ = classify(acct, INDEX)
    assert got_status == status
    assert chosen.person_id == who.id and chosen.method == method


def test_name_pattern_alone_is_queued_not_linked():
    status, chosen, cands = classify(account("pshah"), INDEX)
    assert status == "ambiguous" and chosen is None
    assert cands[0].person_id == PRIYA.id and cands[0].method == "name_pattern"


def test_display_name_alone_is_queued():
    status, chosen, cands = classify(account("u-77", display_name="Shah, Priya"), INDEX)
    assert status == "ambiguous" and cands[0].person_id == PRIYA.id and cands[0].method == "name"


def test_two_people_with_same_pattern_are_ambiguous():
    status, chosen, cands = classify(account("jkim"), INDEX)  # Jordan Kim or Julia Kim
    assert status == "ambiguous" and chosen is None
    assert {c.person_id for c in cands} == {JORDAN_K.id, JULIA_K.id}


def test_service_and_shared_accounts_are_non_human():
    assert classify(account("svc-deploy", account_type=AccountType.service), INDEX)[0] == "non_human"
    assert classify(account("admin", account_type=AccountType.shared), INDEX)[0] == "non_human"


def test_foreign_domain_is_external_and_unknown_is_unmatched():
    assert classify(account("j.doe", email="j.doe@vendor.example"), INDEX)[0] == "external"
    assert classify(account("ghost-user"), INDEX)[0] == "unmatched"


def test_weak_candidates_below_floor_are_dropped():
    assert INDEX.candidates(account("zzz", display_name="Nobody Here")) == []


def test_username_patterns():
    assert username_patterns(["priya", "shah"]) == {"priyashah", "shahpriya", "pshah", "priyas", "shahp"}
    assert username_patterns(["cher"]) == set()

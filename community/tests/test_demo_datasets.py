"""Demo datasets for seed_demo (decision D53): the registry, the default
oak_creek output unchanged, and fresh_start's contents."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import transaction

from accounts.models import User
from audit.models import AuditEvent
from community import demo
from community.models import (
    BoardSeat,
    GoverningRule,
    Lot,
    OfficeHolding,
    Ownership,
    Person,
    Role,
    RoleAssignment,
)
from core.models import DemoSeed
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def run(*args, **kwargs) -> str:
    out = io.StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


# --- Registry ---------------------------------------------------------------------------


def test_registry_lists_both_datasets_with_descriptions():
    assert demo.names()[:2] == ["oak_creek", "fresh_start"]
    assert demo.DEFAULT == "oak_creek"
    for d in demo.DATASETS:
        assert demo.NAME_PATTERN.match(d.name), d.name
        assert d.label and d.description.endswith(".")
        assert callable(__import__(d.module, fromlist=["seed"]).seed)


@pytest.mark.parametrize("bad", ["", "Oak_creek", "oak-creek", "x;rm", "a" * 41, "unknown"])
def test_registry_refuses_names_it_does_not_hold(bad):
    with pytest.raises(KeyError):
        demo.get(bad)


def test_list_datasets_prints_names_one_per_line_without_the_flag(settings):
    settings.SEED_DEMO_ALLOWED = False  # listing loads nothing
    assert run("seed_demo", list_datasets=True).splitlines() == demo.names()


def test_unknown_dataset_is_refused_by_the_command(settings):
    settings.SEED_DEMO_ALLOWED = True
    with pytest.raises(CommandError):
        call_command("seed_demo", "--dataset", "nope", stdout=io.StringIO())
    with system_access("t"):
        assert not Lot.objects.exists()


@pytest.mark.parametrize("dataset", ["oak_creek", "fresh_start"])
def test_every_dataset_refuses_unless_allowed(settings, dataset):
    settings.SEED_DEMO_ALLOWED = False
    with pytest.raises(CommandError, match="SEED_DEMO_ALLOWED"):
        run("seed_demo", dataset=dataset)


# --- oak_creek: the default, output unchanged -------------------------------------------

# Seeded people and lots before the registry existed (names, emails and
# addresses; dates and ledger counts depend on today).
OAK_CREEK_PEOPLE_AND_LOTS = "358db8c2de1ac954021487e1a142b08757512b3eda334102c6eed929f55ba000"
OAK_CREEK_COUNTS = {
    "accounts.User": 5,
    "arc.ArcRequest": 1,
    "comms.Announcement": 2,
    "comms.Event": 4,
    "community.Account": 45,
    "community.AccountContact": 3,
    "community.BoardSeat": 5,
    "community.Lot": 44,
    "community.OfficeHolding": 4,
    "community.Ownership": 47,
    "community.Person": 51,
    "community.RoleAssignment": 3,
    "compliance.Violation": 1,
    "documents.Document": 4,
    "governance.Meeting": 3,
    "governance.Vote": 1,
    "operations.Invoice": 4,
    "operations.Vendor": 3,
}


def _fingerprint() -> tuple[dict, str]:
    counts = {}
    with system_access("fingerprint"):
        for label in OAK_CREEK_COUNTS:
            counts[label] = apps.get_model(label)._base_manager.count()
        people = sorted(Person.objects.values_list("display_name", "email"))
        lots = sorted(Lot.objects.values_list("number", "street_address"))
    digest = hashlib.sha256(json.dumps([people, lots]).encode()).hexdigest()
    return counts, digest


class _Rollback(Exception):
    pass


def test_default_is_oak_creek_and_its_output_is_unchanged(settings):
    settings.SEED_DEMO_ALLOWED = True
    out = run("seed_demo")
    assert "Demo data created: 44 lots, joint owners on lots 7 and 23" in out
    assert "Governing rules are cited 'DEMO - replace'" in out
    assert "Demo logins" in out and "demo-board0@example.com" in out
    default = _fingerprint()
    assert default == (OAK_CREEK_COUNTS, OAK_CREEK_PEOPLE_AND_LOTS)
    assert DemoSeed.objects.get().dataset == "oak_creek"
    event = AuditEvent.objects.get(action="system.demo_seeded")
    assert event.metadata == {"dataset": "oak_creek"}


def test_naming_oak_creek_gives_the_same_data_as_the_default(settings):
    settings.SEED_DEMO_ALLOWED = True
    try:
        with transaction.atomic():
            run("seed_demo")
            default = _fingerprint()
            raise _Rollback
    except _Rollback:
        pass
    with system_access("t"):
        assert not Lot.objects.exists()
    run("seed_demo", dataset="oak_creek")
    assert _fingerprint() == default


# --- fresh_start ------------------------------------------------------------------------


@pytest.fixture
def fresh(settings):
    settings.SEED_DEMO_ALLOWED = True
    return run("seed_demo", dataset="fresh_start")


def test_fresh_start_homes_and_owners(fresh):
    from community.demo import fresh_start

    with system_access("t"):
        assert Lot.objects.count() == 44
        assert len(set(Lot.objects.values_list("street_address", flat=True))) == 44
        assert all(
            a.endswith(fresh_start.STREETS)
            for a in Lot.objects.values_list("street_address", flat=True)
        )
        active = Ownership.objects.active()
        assert active.count() == 46  # two couples
        assert set(active.values_list("lot_id", flat=True)) == set(
            Lot.objects.values_list("pk", flat=True)
        )
        by_type = {
            kind: set(active.filter(ownership_type=kind).values_list("lot__number", flat=True))
            for kind in ("sole", "joint", "trust", "entity")
        }
        assert by_type["joint"] == {7, 23}
        assert by_type["trust"] == {12} and by_type["entity"] == {40}
        assert len(by_type["sole"]) == 40
        no_email = active.filter(person__email="").values_list("lot__number", flat=True)
        assert sorted(no_email) == [9, 26, 41]
        assert Ownership.objects.filter(end_date__isnull=False).count() == 0  # no sales yet


def test_fresh_start_board_offices_and_sysadmin(fresh):
    with system_access("t"):
        seats = BoardSeat.objects.active()
        assert seats.count() == 3
        assert set(seats.values_list("seat_type", flat=True)) == {"declarant_appointed"}
        offices = set(OfficeHolding.objects.active().values_list("office", flat=True))
        assert offices == {"president", "treasurer", "secretary"}
        # The declarant's appointees own no home here.
        board_people = seats.values_list("person_id", flat=True)
        assert not Ownership.objects.filter(person_id__in=board_people).exists()
        roles = RoleAssignment.objects.active()
        assert list(roles.values_list("role", flat=True)) == [Role.SYSADMIN]


def test_fresh_start_has_no_history_and_default_settings(fresh):
    from accounts.models import Invitation
    from arc.models import ArcRequest
    from comms.models import Announcement, Event
    from compliance.models import Violation
    from documents.models import Document
    from governance.models import Meeting, Vote
    from ledger.models import LedgerEntry
    from operations.models import Invoice, OwnerRequest, Vendor, WorkOrder

    with system_access("t"):
        for model in (
            LedgerEntry,
            ArcRequest,
            Violation,
            Meeting,
            Vote,
            OwnerRequest,
            WorkOrder,
            Invoice,
            Vendor,
            Announcement,
            Event,
            Invitation,
        ):
            assert not model._base_manager.exists(), model.__name__
        docs = list(Document.objects.values_list("title", "category"))
        assert docs == [("Declaration (demo placeholder)", "governing")]
        # Community settings left for the board to fill in.
        assert not GoverningRule.objects.exclude(value="").exists()
    assert DemoSeed.objects.get().dataset == "fresh_start"


def test_fresh_start_is_fictional_only(fresh):
    with system_access("t"):
        emails = [e for e in Person.objects.values_list("email", flat=True) if e]
        names = list(Person.objects.values_list("display_name", flat=True))
    emails += list(User.objects.values_list("email", flat=True))
    assert emails and all(demo.is_demo_email(e) for e in emails), emails
    assert all(n.endswith(demo.DEMO_SUFFIX) for n in names)
    # The one login is the Declaration's author, with no way to sign in.
    users = list(User.objects.all())
    assert len(users) == 1 and not users[0].has_usable_password()


def test_fresh_start_prints_logins_and_who_to_invite(fresh):
    assert "Fresh start created: 44 homes" in fresh
    assert "Demo logins" in fresh and "fresh-board0@example.com" in fresh
    assert "People to invite first" in fresh and "sysadmin, Robin Sysadmin (demo)" in fresh


def test_fresh_start_refuses_on_top_of_existing_lots(fresh):
    with pytest.raises(CommandError, match="already exist"):
        run("seed_demo", dataset="fresh_start")


def test_fresh_start_pages_load_for_a_board_member(client, fresh):
    from django.urls import reverse

    from community.tests.factories import make_member

    client.force_login(make_member(board=True))
    for name in ("my_items", "community_settings", "lot_list", "documents", "ledger_home"):
        assert client.get(reverse(name)).status_code == 200, name

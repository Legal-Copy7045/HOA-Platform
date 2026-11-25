import io
import re

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from accounts.models import User
from audit.models import AuditEvent
from community.models import (
    BoardSeat,
    Lot,
    OfficeHolding,
    Ownership,
    Person,
    Role,
    RoleAssignment,
)
from community.tests.factories import LotFactory, make_member
from core import policy
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def run(*args, **kwargs) -> str:
    out = io.StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


# --- bootstrap_first_officer ------------------------------------------------------


def test_bootstrap_creates_officer_sysadmin_and_a_working_link(client):
    out = run(
        "bootstrap_first_officer",
        name="First Officer",
        email="first@example.com",
        also_sysadmin=True,
    )
    link = re.search(r"(http\S+/invite/\S+/)", out).group(1)
    with system_access("test"):
        person = Person.objects.get(email="first@example.com")
        assert BoardSeat.objects.filter(person=person).active().exists()
        assert OfficeHolding.objects.get(person=person).office == "president"
        assert RoleAssignment.objects.get(person=person).role == Role.SYSADMIN
    path = link.split("localhost:8000", 1)[1]
    client.get(path)
    client.post(
        reverse("invite_accept"),
        {"password1": "a long fresh passphrase", "password2": "a long fresh passphrase"},
    )
    user = User.objects.get(email="first@example.com")
    assert policy.can(user, "seat.manage") and policy.is_sysadmin(user)
    assert AuditEvent.objects.filter(action="system.bootstrap").exists()


def test_bootstrap_refuses_once_a_board_exists():
    make_member(board=True)
    with pytest.raises(CommandError, match="already exists"):
        run("bootstrap_first_officer", name="X", email="x@example.com")


def test_bootstrap_does_not_create_a_second_sysadmin():
    make_member(roles=[Role.SYSADMIN])
    with pytest.raises(CommandError, match="sysadmin already exists"):
        run("bootstrap_first_officer", name="X", email="x@example.com", also_sysadmin=True)


# --- break-glass ---------------------------------------------------------------------


@pytest.fixture
def sysadmin():
    return make_member(roles=[Role.SYSADMIN])


def test_breakglass_requires_a_sysadmin_actor_and_a_reason(sysadmin):
    lot = LotFactory()
    make_member(lots=[lot])
    with system_access("t"):
        ownership = Ownership.objects.get(lot=lot)
    owner = make_member(lots=[LotFactory()])
    with pytest.raises(CommandError, match="active sysadmin"):
        run(
            "breakglass_end_ownership",
            ownership=ownership.pk,
            actor=owner.email,
            reason="testing the safeguard",
        )
    with pytest.raises(CommandError, match="reason"):
        run(
            "breakglass_end_ownership", ownership=ownership.pk, actor=sysadmin.email, reason="short"
        )
    run(
        "breakglass_end_ownership",
        ownership=ownership.pk,
        actor=sysadmin.email,
        reason="duplicate record from import",
    )
    ownership.refresh_from_db()
    assert ownership.end_date is not None
    event = AuditEvent.objects.get(action="sysadmin.breakglass")
    assert event.actor == sysadmin and event.channel == "cli"
    assert event.metadata["command"] == "breakglass_end_ownership"


def test_breakglass_lock_user_signs_out_and_deactivates(client, sysadmin):
    victim = make_member(lots=[LotFactory()])
    client.force_login(victim)
    run(
        "breakglass_lock_user",
        user=victim.pk,
        actor=sysadmin.email,
        reason="suspected account compromise",
    )
    victim.refresh_from_db()
    assert not victim.is_active
    response = client.get(reverse("home"))
    assert response.wsgi_request.user.is_anonymous  # the old session no longer works


def test_breakglass_events_appear_on_the_board_activity_page(client, sysadmin):
    target = make_member(roles=[Role.AUDITOR])
    with system_access("t"):
        assignment = RoleAssignment.objects.get(person__user=target)
    run(
        "breakglass_end_role",
        assignment=assignment.pk,
        actor=sysadmin.email,
        reason="auditor engagement finished",
    )
    client.force_login(make_member(board=True))
    page = client.get(reverse("sysadmin_activity")).content.decode()
    assert "sysadmin.breakglass" in page and "auditor engagement finished" in page


# --- seed_demo ------------------------------------------------------------------------


def test_seed_demo_refuses_unless_allowed(settings):
    settings.SEED_DEMO_ALLOWED = False
    with pytest.raises(CommandError, match="SEED_DEMO_ALLOWED"):
        run("seed_demo")


def test_seed_demo_builds_the_fictional_community(settings):
    settings.SEED_DEMO_ALLOWED = True
    run("seed_demo")
    with system_access("t"):
        assert Lot.objects.count() == 44
        assert Ownership.objects.active().filter(lot__number=7).count() == 2
        assert Ownership.objects.active().filter(lot__number=23).count() == 2
        lot31 = Ownership.objects.filter(lot__number=31)
        assert lot31.count() == 2 and lot31.active().count() == 1
        assert BoardSeat.objects.active().count() == 5
        assert OfficeHolding.objects.active().count() == 4
        assert RoleAssignment.objects.filter(role=Role.ARC_REVIEWER).count() == 2
        assert RoleAssignment.objects.filter(role=Role.AUDITOR).count() == 1
        assert all(
            e.endswith("@example.com") for e in Person.objects.values_list("email", flat=True)
        )
        # Only a few demo people get logins (authors of demo records): fictional
        # @example.com addresses with no usable password.
        demo_logins = User.objects.filter(person__isnull=False)
        assert all(u.email.endswith("@example.com") for u in demo_logins)
        assert not any(u.has_usable_password() for u in demo_logins)
    with pytest.raises(CommandError, match="already exist"):
        run("seed_demo")


def test_seed_demo_after_bootstrap_fills_only_free_offices(settings):
    settings.SEED_DEMO_ALLOWED = True
    run("bootstrap_first_officer", name="First Officer", email="first@example.com")
    run("seed_demo")
    with system_access("t"):
        assert BoardSeat.objects.active().count() == 5
        assert OfficeHolding.objects.active().filter(office="president").count() == 1
        assert OfficeHolding.objects.active().count() == 4


def test_print_invitation_link_replaces_a_lost_bootstrap_link(client):
    first = run("bootstrap_first_officer", name="First Officer", email="first@example.com")
    old = re.search(r"localhost:8000(/invite/\S+/)", first).group(1)
    out = run("print_invitation_link", email="FIRST@example.com")
    new = re.search(r"localhost:8000(/invite/\S+/)", out).group(1)
    assert client.get(old).status_code == 404
    assert client.get(new).status_code == 302
    with pytest.raises(CommandError):
        run("print_invitation_link", email="nobody@example.com")


def test_print_invitation_link_refuses_once_anyone_has_a_login():
    """Review E7: after bootstrap, a printed link would hand whoever runs the
    command another person's identity (for example a newly elected officer)."""
    from community.tests.factories import PersonFactory, make_member

    make_member(board=True)
    PersonFactory(email="new.officer@example.com")
    with pytest.raises(CommandError, match="already has logins"):
        run("print_invitation_link", email="new.officer@example.com")


def test_web_and_worker_refuse_to_run_as_the_database_owner(settings):
    """Review E6: the test database connection is the owner role."""
    with pytest.raises(CommandError, match="not the runtime role"):
        call_command("check_db_role")
    settings.DB_APP_ROLE = ""
    call_command("check_db_role")  # single-role databases are allowed to skip


def test_seed_demo_includes_operations_and_every_page_loads(client, settings):
    """The whole demo community renders on every board and owner page."""
    from django.urls import reverse

    from ledger.models import LedgerEntry
    from operations.models import Invoice, Vendor

    settings.SEED_DEMO_ALLOWED = True
    run("seed_demo")
    assert Vendor.objects.count() == 3
    assert Invoice.objects.filter(state="paid").exists()
    with system_access("t"):
        assert LedgerEntry.objects.filter(kind="assessment").exists()
    with pytest.raises(CommandError, match="already exists"):
        run("seed_demo_operations")
    client.force_login(make_member(board=True))
    for name in (
        "board_home",
        "community_settings",
        "ledger_home",
        "collections",
        "invoices",
        "work_orders",
        "vendors",
        "budgets",
        "reports",
        "arc_list",
        "violations",
        "meetings",
        "votes",
        "resolutions",
        "records_requests",
        "announcements",
        "calendar",
        "documents",
        "resales",
        "tasks",
    ):
        assert client.get(reverse(name)).status_code == 200, name


def test_rescan_blocks_a_file_found_infected_later(client, sysadmin, settings):
    """Review C2: attachments could never be rescanned or quarantined."""
    from unittest import mock

    from arc import services as arc
    from documents import attachments
    from documents.tests.helpers import pdf

    lot = LotFactory()
    owner = make_member(lots=[lot])
    req = arc.create_request(lot=lot, title="Fence", description="x", actor=owner)
    file = attachments.attach(req, pdf(), actor=owner)
    client.force_login(owner)
    url = reverse("arc_file", args=[req.pk, file.pk])
    assert client.get(url).status_code == 200
    with mock.patch(
        "documents.management.commands.rescan_files.scan", return_value=("infected", "clamd")
    ):
        out = run("rescan_files", actor=sysadmin.email, reason="signatures updated today")
    assert "infected: 1" in out
    assert client.get(url).status_code == 404

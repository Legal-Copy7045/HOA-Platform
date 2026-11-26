"""Every board member is also a homeowner: their ownership comes from the
register like anyone's, the board seat stacks on top, and ending the seat
leaves the homeowner access (owner review 2026-11-25)."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from audit.models import AuditEvent
from community import services
from community.models import BoardSeat, Lot, Ownership, Person
from community.tests.factories import LotFactory, days_ago, make_member
from core import policy
from core.scoping import system_access

pytestmark = pytest.mark.django_db

HEADER = "lot,street_address,owner_name,owner_email,owner_since,ownership_type\n"


def _csv(tmp_path, body):
    path = tmp_path / "register.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return str(path)


# --- The register import -------------------------------------------------------------------


def test_check_only_writes_nothing(tmp_path):
    path = _csv(tmp_path, "1,100 Example Lane,Pat Example,pat@example.com,2019-06-14,sole\n")
    out = StringIO()
    call_command("import_register", path, stdout=out)
    assert "Check only" in out.getvalue()
    with system_access("t"):
        assert not Lot.objects.exists()


def test_import_creates_lots_people_and_joint_owners(tmp_path):
    path = _csv(
        tmp_path,
        "1,100 Example Lane,Pat Example,pat@example.com,2019-06-14,\n"
        "2,102 Example Lane,Sam Sample,sam@example.com,2021-03-02,\n"
        "2,102 Example Lane,Alex Sample,,2021-03-02,\n",
    )
    out = StringIO()
    call_command("import_register", path, "--commit", stdout=out)
    text = out.getvalue()
    assert "pat@example.com" not in text and "Pat Example" not in text  # no personal data
    with system_access("t"):
        assert Lot.objects.count() == 2
        lot2 = Ownership.objects.active().filter(lot__number=2)
        assert {o.ownership_type for o in lot2} == {"joint"}
        assert Ownership.objects.get(lot__number=1).ownership_type == "sole"
    assert AuditEvent.objects.filter(action="register.imported").exists()


def test_import_links_the_first_officer_by_email(tmp_path):
    officer = make_member(board=True, email="officer@example.com")
    with system_access("t"):
        Person.objects.filter(user=officer).update(email="officer@example.com")
    path = _csv(tmp_path, "7,112 Example Lane,Officer Name,OFFICER@example.com,2018-01-05,sole\n")
    call_command("import_register", path, "--commit", stdout=StringIO())
    roles = policy.roles_for(officer)
    assert {policy.OWNER, policy.BOARD} <= roles.groups


def test_bad_rows_are_reported_by_number_and_nothing_written(tmp_path):
    path = _csv(
        tmp_path,
        "x,100 Example Lane,Pat,,2019-06-14,\n"
        "2,,Sam,not-an-email,2019-13-01,\n"
        "3,104 Example Lane,Kim,,2019-06-14,castle\n",
    )
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("import_register", path, "--commit", stdout=out)
    text = out.getvalue()
    assert "row 2" in text and "row 3" in text and "row 4" in text
    with system_access("t"):
        assert not Lot.objects.exists()


def test_lots_with_current_owners_are_left_alone(tmp_path):
    lot = LotFactory(number=5)
    make_member(lots=[lot])
    path = _csv(tmp_path, "5,108 Example Lane,Someone Else,,2020-01-01,sole\n")
    out = StringIO()
    call_command("import_register", path, "--commit", stdout=out)
    assert "lot 5: already has current owners" in out.getvalue()
    with system_access("t"):
        assert Ownership.objects.active().filter(lot=lot).count() == 1


# --- Board members are homeowners first ---------------------------------------------------


@pytest.fixture
def board_owner():
    lot = LotFactory()
    return make_member(board=True, lots=[lot]), lot


def test_board_member_has_both_homeowner_and_board_sides(client, board_owner):
    user, _lot = board_owner
    roles = policy.roles_for(user)
    assert {policy.OWNER, policy.BOARD} <= roles.groups
    assert policy.can(user, "arc.submit") and policy.can(user, "vote.cast")
    assert policy.can(user, "invoice.approve")
    client.force_login(user)
    page = client.get(reverse("home")).content.decode()
    assert ">My home<" in page and ">Association<" in page


def test_retiring_from_the_board_keeps_homeowner_access(client, board_owner):
    user, lot = board_owner
    with system_access("t"):
        seat = BoardSeat.objects.get(person__user=user)
    other = make_member(board=True)
    services.end_board_seat(seat, ended_on=days_ago(0), reason="term_expired", actor=other)
    roles = policy.roles_for(user)
    assert policy.BOARD not in roles.groups
    assert policy.OWNER in roles.groups and lot.pk in roles.owned_lot_ids
    assert policy.can(user, "arc.submit") and not policy.can(user, "invoice.approve")
    client.force_login(user)
    page = client.get(reverse("home")).content.decode()
    assert ">My home<" in page and ">Association<" not in page
    assert client.get(reverse("my_lot")).status_code == 200
    assert client.get(reverse("board_home")).status_code == 403


def test_a_board_member_cannot_record_their_own_ownership(board_owner):
    user, _lot = board_owner
    with system_access("t"):
        me = Person.objects.get(user=user)
    with pytest.raises(services.CommunityError):
        services.record_ownership(person=me, lot=LotFactory(), start_date=days_ago(1), actor=user)
    # Another board member records it, like any owner's.
    other = make_member(board=True)
    services.record_ownership(person=me, lot=LotFactory(), start_date=days_ago(1), actor=other)

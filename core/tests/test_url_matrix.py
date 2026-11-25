"""Every URL is classified and cross-owner access is refused everywhere.

CLAUDE.md: "Every feature ships with cross-owner access tests: owner A must
get 403/404 for owner B's lot ... on every URL and download path."

Adding a URL without classifying it here fails
test_every_named_url_is_classified - by design.
"""

from unittest import mock

import pytest
from django.urls import get_resolver, reverse

from accounts import mfa_reset
from accounts import services as account_services
from accounts.models import Invitation, MfaReset, VerificationMethod
from community import services
from community.models import (
    BoardSeat,
    Office,
    OfficeHolding,
    Ownership,
    Person,
    Role,
    RoleAssignment,
    SysadminNomination,
)
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db

PUBLIC = {"home", "healthz", "invite_landing", "invite_accept"}
# Signed-in account pages provided by allauth (each acts only on the signed-in
# user's own account) plus our enrollment-code page.
OWN_ACCOUNT_PREFIXES = ("account_", "mfa_")
OWN_ACCOUNT = {"enroll_with_code"}

# name -> (method, kwargs builder key). Every one must refuse owner A when
# pointed at owner B's data, and send anonymous visitors to sign in.
PROTECTED = {
    "my_lot": ("GET", None),
    "lot_list": ("GET", None),
    "lot_detail": ("GET", "lot"),
    "lot_transfer": ("POST", "lot"),
    "board_home": ("GET", None),
    "approvals": ("GET", None),
    "people": ("GET", None),
    "person_detail": ("GET", "person"),
    "person_add_ownership": ("POST", "person"),
    "person_add_seat": ("POST", "person"),
    "person_add_office": ("POST", "person"),
    "person_add_role": ("POST", "person"),
    "sysadmin_nominate": ("POST", "person"),
    "ownership_end": ("POST", "ownership"),
    "seat_end": ("POST", "seat"),
    "office_end": ("POST", "office"),
    "role_end": ("POST", "role"),
    "nomination_decide": ("POST", "nomination"),
    "invitations": ("GET", None),
    "invite_person": ("POST", "person"),
    "invitation_action": ("POST", "invitation"),
    "reset_start": ("POST", "person"),
    "reset_decide": ("POST", "reset"),
    "sysadmin_activity": ("GET", None),
    "system_status": ("GET", None),
    "community_settings": ("GET", None),
    "community_rule_save": ("POST", None),
    "community_profile_save": ("POST", None),
    "common_area_save": ("POST", None),
}


def _named_urls() -> set[str]:
    return {k for k in get_resolver().reverse_dict if isinstance(k, str)}


def test_every_named_url_is_classified():
    unclassified = {
        name
        for name in _named_urls()
        if name not in PUBLIC | OWN_ACCOUNT | set(PROTECTED)
        and not name.startswith(OWN_ACCOUNT_PREFIXES)
    }
    assert unclassified == set(), f"Classify these URLs in {__name__}: {sorted(unclassified)}"


@pytest.fixture
def world():
    with (
        mock.patch("accounts.tasks.send_invitation.defer"),
        mock.patch("community.tasks.notify_board.defer"),
    ):
        lot_a, lot_b = (
            LotFactory(street_address="1 Alpha Way"),
            LotFactory(street_address="2 Bravo Way"),
        )
        owner_a = make_member(lots=[lot_a])
        owner_b = make_member(lots=[lot_b], board=True, roles=[Role.ARC_REVIEWER])
        board = make_member(board=True)
        board2 = make_member(board=True)
        person_b = Person.objects.unscoped("t").get(user=owner_b)
        services.record_office(
            person=person_b, office=Office.TREASURER, start_date=days_ago(5), actor=SYSTEM
        )
        nomination = services.nominate_sysadmin(person=person_b, actor=board)
        uninvited = PersonFactory()
        services.record_ownership(person=uninvited, lot=lot_b, start_date=days_ago(2), actor=SYSTEM)
        invitation = account_services.create_invitation(person=uninvited, actor=board)
        reset = mfa_reset.start_reset(
            subject=owner_b, actor=board, verification_method=VerificationMethod.IN_PERSON
        )
    targets = {
        "lot": lot_b.pk,
        "person": person_b.pk,
        "ownership": Ownership.objects.unscoped("t").get(person=person_b).pk,
        "seat": BoardSeat.objects.unscoped("t").get(person=person_b).pk,
        "office": OfficeHolding.objects.unscoped("t").get(person=person_b).pk,
        "role": RoleAssignment.objects.unscoped("t").get(person=person_b).pk,
        "nomination": SysadminNomination.objects.unscoped("t").get(pk=nomination.pk).pk,
        "invitation": Invitation.objects.unscoped("t").get(pk=invitation.pk).pk,
        "reset": MfaReset.objects.unscoped("t").get(pk=reset.pk).pk,
    }
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "board": board,
        "board2": board2,
        "lot_a": lot_a,
        "lot_b": lot_b,
        "targets": targets,
    }


def _url(name, key, targets):
    kwargs = {}
    if key:
        kwargs["pk"] = targets[key]
    if name == "nomination_decide" or name == "reset_decide":
        kwargs["decision"] = "approve"
    elif name == "invitation_action":
        kwargs["action"] = "revoke"
    return reverse(name, kwargs=kwargs)


@pytest.mark.parametrize("name", sorted(PROTECTED))
def test_owner_a_cannot_reach_owner_b_data(client, world, name):
    method, key = PROTECTED[name]
    client.force_login(world["owner_a"])
    url = _url(name, key, world["targets"])
    response = client.get(url) if method == "GET" else client.post(url, {})
    if name == "my_lot":  # their own page: allowed, but must not show B's lot
        assert response.status_code == 200
        assert b"2 Bravo Way" not in response.content
        assert b"1 Alpha Way" in response.content
    else:
        assert response.status_code in (403, 404), f"{name} -> {response.status_code}"
    # Nothing changed as a side effect.
    assert Ownership.objects.unscoped("t").filter(end_date__isnull=False).count() == 0
    assert not RoleAssignment.objects.unscoped("t").filter(role=Role.SYSADMIN).exists()


@pytest.mark.parametrize("name", sorted(PROTECTED))
def test_anonymous_visitors_are_sent_to_sign_in(client, world, name):
    method, key = PROTECTED[name]
    url = _url(name, key, world["targets"])
    response = client.get(url) if method == "GET" else client.post(url, {})
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_owner_sees_own_lot_detail(client, world):
    client.force_login(world["owner_a"])
    response = client.get(reverse("lot_detail", args=[world["lot_a"].pk]))
    assert response.status_code == 200
    assert b"1 Alpha Way" in response.content
    assert b"Transfer this lot" not in response.content  # board-only controls


@pytest.mark.parametrize(
    ("who", "allowed", "denied"),
    [
        (
            "auditor",
            ["lot_list", "sysadmin_activity"],
            ["board_home", "people", "system_status", "invitations", "approvals"],
        ),
        (
            "sysadmin",
            ["system_status", "invitations"],
            ["lot_list", "board_home", "people", "sysadmin_activity", "my_lot"],
        ),
        ("arc", [], ["lot_list", "board_home", "people", "system_status", "my_lot"]),
    ],
)
def test_role_page_access(client, world, who, allowed, denied):
    user = make_member(
        roles=[{"auditor": Role.AUDITOR, "sysadmin": Role.SYSADMIN, "arc": Role.ARC_REVIEWER}[who]]
    )
    client.force_login(user)
    for name in allowed:
        assert client.get(reverse(name)).status_code == 200, name
    for name in denied:
        assert client.get(reverse(name)).status_code == 403, name


def test_board_pages_load(client, world):
    client.force_login(world["board"])
    for name in (
        "board_home",
        "people",
        "approvals",
        "invitations",
        "lot_list",
        "sysadmin_activity",
    ):
        assert client.get(reverse(name)).status_code == 200, name
    for key, name in (("person", "person_detail"), ("lot", "lot_detail")):
        assert client.get(reverse(name, args=[world["targets"][key]])).status_code == 200


def test_transfer_through_the_page_revokes_the_seller_at_once(client, world):
    buyer = PersonFactory()
    client.force_login(world["board"])
    client.post(
        reverse("lot_transfer", args=[world["lot_a"].pk]),
        {"new_owners": [buyer.pk], "transfer_date": days_ago(0).isoformat()},
    )
    client.force_login(world["owner_a"])
    assert client.get(reverse("lot_detail", args=[world["lot_a"].pk])).status_code == 404
    assert client.get(reverse("my_lot")).status_code == 403


def test_sysadmin_requests_are_audited_and_shown_to_the_board(client, world):
    from audit.models import AuditEvent

    admin = make_member(roles=[Role.SYSADMIN])
    client.force_login(admin)
    client.get(reverse("system_status"))
    event = AuditEvent.objects.filter(action="sysadmin.request", actor=admin).latest("id")
    assert event.metadata["view"] == "system_status"
    client.force_login(world["board"])
    page = client.get(reverse("sysadmin_activity")).content.decode()
    assert "sysadmin.request" in page and admin.email in page


def test_reset_code_is_shown_once_to_the_approver(client, world):
    from audit.models import AuditEvent

    reset_id = world["targets"]["reset"]
    client.force_login(world["board2"])
    with mock.patch("community.tasks.notify_board.defer"):
        response = client.post(reverse("reset_decide", args=[reset_id, "approve"]))
    assert response.status_code == 200
    code = response.context["code"]
    assert code.encode() in response.content
    assert response["Cache-Control"].startswith("max-age=0")
    assert code not in str(list(AuditEvent.objects.values_list("metadata", flat=True)))

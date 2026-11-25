"""Homeowner view: the board sees exactly what one lot's owner sees, read-only."""

import pytest
from django.urls import reverse

from audit.models import AuditEvent
from community.tests.factories import LotFactory, days_ago, make_member
from core import policy

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot, other = LotFactory(), LotFactory()
    return {
        "lot": lot,
        "other": other,
        "board": make_member(board=True),
        "owner": make_member(lots=[lot]),
        "owner_other": make_member(lots=[other]),
    }


def _start(client, user, lot):
    client.force_login(user)
    return client.post(reverse("owner_preview_start"), {"lot": lot.pk})


def test_board_sees_the_owners_menu_and_scope(client, w):
    _start(client, w["board"], w["lot"])
    page = client.get(reverse("home"))
    assert b"Homeowner view" in page.content
    assert b"Board overview" not in page.content  # board menu gone
    assert client.get(reverse("board_home")).status_code == 403
    assert client.get(reverse("my_lot")).status_code == 200
    assert client.get(reverse("lot_detail", args=[w["other"].pk])).status_code in (403, 404)
    assert AuditEvent.objects.filter(action="preview.owner_started", actor=w["board"]).exists()


def test_homeowner_view_is_read_only(client, w):
    _start(client, w["board"], w["lot"])
    response = client.post(reverse("my_profile"), {"phone": "x"})
    assert response.status_code == 403


def test_exit_restores_the_board_view(client, w):
    _start(client, w["board"], w["lot"])
    client.post(reverse("owner_preview_stop"))
    assert client.get(reverse("board_home")).status_code == 200
    assert AuditEvent.objects.filter(action="preview.owner_stopped").exists()


def test_owners_cannot_use_homeowner_view(client, w):
    assert _start(client, w["owner"], w["other"]).status_code == 403


def test_preview_affects_only_the_viewer(client, w):
    _start(client, w["board"], w["lot"])
    assert policy.can(w["owner_other"], "vote.cast")  # other users unchanged
    assert not policy.roles_for(w["owner_other"]).owned_lot_ids & {w["lot"].pk}


def test_preview_ends_if_the_viewer_loses_the_role(client, w):
    from community.models import BoardSeat
    from core.scoping import system_access

    _start(client, w["board"], w["lot"])
    with system_access("t"):
        BoardSeat.objects.filter(person__user=w["board"]).update(
            ended_on=days_ago(1), end_reason="resigned"
        )
    page = client.get(reverse("home"))
    assert b"Homeowner view" not in page.content

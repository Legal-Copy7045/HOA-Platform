from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from compliance import services
from compliance.models import Violation, ViolationState
from core.workflow import WorkflowError

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot = LotFactory()
    board = make_member(board=True)
    community.set_rule(
        key="violation.cure_days", value="14", citation="Declaration §11.2", actor=board
    )
    manager = make_member(roles=[Role.MANAGER])
    return {
        "board": board,
        "manager": manager,
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "other": make_member(lots=[LotFactory()]),
        "v": services.report(lot=lot, category="trash", description="Bins at curb", actor=manager),
    }


def test_notice_sets_cure_date_and_letter_cites_rule(w):
    v = services.VIOLATION.apply(w["v"], "send_notice", actor=w["manager"])
    assert v.cure_by == timezone.localdate() + timedelta(days=14)
    letter = services.notice_letter(v)
    assert letter.startswith(b"%PDF") and b"No fine has been assessed" in letter


def test_owner_responds_others_cannot(w):
    services.VIOLATION.apply(w["v"], "send_notice", actor=w["manager"])
    with pytest.raises(WorkflowError):
        services.respond(w["v"], text="Not mine", actor=w["other"])
    services.respond(w["v"], text="Moved them today", actor=w["owner"])
    w["v"].refresh_from_db()
    assert w["v"].state == ViolationState.RESPONDED


def test_only_board_decides_and_needs_a_note(w):
    services.VIOLATION.apply(w["v"], "send_notice", actor=w["manager"])
    with pytest.raises(PermissionDenied):
        services.VIOLATION.apply(w["v"], "dismiss", actor=w["manager"], note="x")
    with pytest.raises(WorkflowError, match="note"):
        services.VIOLATION.apply(w["v"], "dismiss", actor=w["board"])
    services.VIOLATION.apply(w["v"], "dismiss", actor=w["board"], note="Collected same day")


def test_owners_see_only_their_lot(client, w):
    assert list(Violation.objects.for_user(w["owner"])) == [w["v"]]
    assert not Violation.objects.for_user(w["other"]).exists()
    client.force_login(w["other"])
    assert client.get(reverse("violation_detail", args=[w["v"].pk])).status_code == 404


def test_owners_cannot_report():
    with pytest.raises(PermissionDenied):
        services.report(
            lot=LotFactory(),
            category="trash",
            description="x",
            actor=make_member(lots=[LotFactory()]),
        )

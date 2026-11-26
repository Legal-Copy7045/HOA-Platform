"""Marking a violation cured is the board's decision, after a hearing or
otherwise; the manager cannot (owner decision 2026-11-26, D49)."""

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from community.models import Role
from community.tests.factories import LotFactory, make_member
from compliance import services
from compliance.models import Violation
from core import policy

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    board = make_member(board=True)
    v = services.report(lot=LotFactory(), category="parking", description="Boat", actor=board)
    services.VIOLATION.apply(v, "send_notice", actor=board)
    return {"board": board, "manager": make_member(roles=[Role.MANAGER]), "v": v}


def _state(v):
    return Violation.objects.unscoped("t").get(pk=v.pk).state


def test_mark_cured_needs_the_board_permission():
    t = services.VIOLATION.transitions["mark_cured"]
    assert t.permission == "violation.decide"
    assert policy.PERMISSIONS["violation.decide"] == frozenset({policy.BOARD})


@pytest.mark.parametrize("state_step", [None, "schedule_hearing"])
def test_manager_cannot_mark_cured_by_service(w, state_step):
    if state_step:
        services.VIOLATION.apply(w["v"], state_step, note="June 3, clubhouse", actor=w["board"])
    with pytest.raises(PermissionDenied):
        services.VIOLATION.apply(w["v"], "mark_cured", actor=w["manager"])
    assert _state(w["v"]) != "cured"


def test_manager_cannot_mark_cured_through_the_page(client, w):
    client.force_login(w["manager"])
    page = client.get(reverse("violation_detail", args=[w["v"].pk])).content.decode()
    assert "Mark cured" not in page
    assert "Schedule hearing" in page  # the manager keeps the data-entry steps
    response = client.post(reverse("violation_action", args=[w["v"].pk, "mark_cured"]))
    assert response.status_code == 403
    assert _state(w["v"]) == "noticed"


def test_board_marks_cured_after_a_hearing(client, w):
    services.VIOLATION.apply(w["v"], "schedule_hearing", note="June 3", actor=w["board"])
    client.force_login(w["board"])
    page = client.get(reverse("violation_detail", args=[w["v"].pk])).content.decode()
    assert "Mark cured (board)" in page
    client.post(reverse("violation_action", args=[w["v"].pk, "mark_cured"]))
    assert _state(w["v"]) == "cured"
    # Closing the cured record stays a data-entry step the manager may take.
    client.force_login(w["manager"])
    client.post(reverse("violation_action", args=[w["v"].pk, "close"]))
    assert _state(w["v"]) == "closed"

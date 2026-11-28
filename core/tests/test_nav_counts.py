"""Sidebar count bubbles: each menu entry shows how many things there are
waiting on you (owner review 2026-11-25)."""

import re

import pytest
from django.urls import reverse

from arc import services as arc
from community.models import Role
from community.tests.factories import LotFactory, make_member
from compliance import services as compliance
from core import my_items

pytestmark = pytest.mark.django_db


def _bubble(html: str, label: str):
    """The number in the bubble after a menu label, or None."""
    m = re.search(
        rf"<span>{re.escape(label)}</span>(?:<span class=\"badge-count\"[^>]*>(\d+)<)?", html
    )
    assert m, f"menu entry {label!r} not found"
    return int(m.group(1)) if m.group(1) else None


@pytest.fixture
def w():
    lot_a, lot_b, lot_c = LotFactory(), LotFactory(), LotFactory()
    reviewer = make_member(roles=[Role.ARC_REVIEWER], board=True, lots=[lot_c])
    owner_a, owner_b = make_member(lots=[lot_a]), make_member(lots=[lot_b])
    board = make_member(board=True)
    for owner, lot in ((owner_a, lot_a), (owner_b, lot_b)):
        req = arc.create_request(lot=lot, title="Fence", description="Rear", actor=owner)
        arc.ARC.apply(req, "submit", actor=owner)
        arc.ARC.apply(req, "accept", actor=board)
    return {"reviewer": reviewer, "owner_a": owner_a, "board": board, "lot_a": lot_a}


def test_two_arc_requests_to_decide_show_2(client, w):
    client.force_login(w["reviewer"])
    html = client.get(reverse("home")).content.decode()
    assert _bubble(html, "Architectural review") == 2
    assert _bubble(html, "My items") >= 2


def test_bubble_goes_away_once_you_take_a_position(client, w):
    from arc.models import ArcRequest

    req = ArcRequest.objects.for_user(w["reviewer"]).filter(state="in_review").first()
    arc.record_position(req, position="approve", comment="ok", actor=w["reviewer"])
    assert my_items.nav_counts(w["reviewer"]).get("arc_list") == 1


def test_owner_gets_bubbles_for_their_own_notices_only(client, w):
    v = compliance.report(lot=w["lot_a"], category="trash", description="Bins", actor=w["board"])
    compliance.VIOLATION.apply(v, "send_notice", actor=w["board"])
    client.force_login(w["owner_a"])
    html = client.get(reverse("home")).content.decode()
    assert _bubble(html, "My violations") == 1
    assert "Architectural review<" not in html  # no board entries, so no board counts


def test_no_bubble_when_nothing_is_waiting(client, w):
    client.force_login(make_member(lots=[LotFactory()]))
    html = client.get(reverse("home")).content.decode()
    assert 'class="badge-count"' not in html

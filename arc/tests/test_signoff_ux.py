"""The neighbor's review page and the applicant's acknowledgement panel after
the UX review (signed-in only, D24)."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from arc import services as arc
from arc import signoffs
from arc.models import SignoffRequest, SignoffResponse
from community import services as community
from community.models import CommunityProfile
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access
from documents.attachments import attach
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(settings, django_capture_on_commit_callbacks):
    settings.PORTAL_BASE_URL = "https://portal.example.com"
    applicant_lot, left_lot, far_lot = LotFactory(), LotFactory(), LotFactory()
    board = make_member(board=True)
    applicant = make_member(lots=[applicant_lot])
    neighbor = make_member(lots=[left_lot])
    signoffs.set_neighbors(applicant_lot, left=left_lot, actor=board)
    req = arc.create_request(
        lot=applicant_lot, title="Rear fence", description="4 ft aluminum", actor=applicant
    )
    attach(req, pdf("plans.pdf"), actor=applicant)
    with django_capture_on_commit_callbacks(execute=True):
        arc.ARC.apply(req, "request_acknowledgements", actor=applicant)
    with system_access("t"):
        signoff = SignoffRequest.objects.get(arc_request=req, lot=left_lot)
    return {
        "board": board,
        "applicant": applicant,
        "neighbor": neighbor,
        "outsider": make_member(lots=[far_lot]),
        "req": req,
        "signoff": signoff,
    }


def _page(client, w):
    client.force_login(w["neighbor"])
    return client.get(reverse("signoff_detail", args=[w["signoff"].pk])).content.decode()


def test_review_page_stacks_plans_above_choice_cards(client, w):
    page = _page(client, w)
    assert 'class="split"' not in page
    assert page.index("Plans and photos") < page.index("Your acknowledgement")
    assert 'class="choice"' in page and 'class="choices"' in page
    assert 'data-required-if="position=objected"' in page
    assert "You were asked because you live next door (left side)." in page


def test_objection_without_comment_keeps_what_was_typed(client, w):
    client.force_login(w["neighbor"])
    response = client.post(
        reverse("signoff_detail", args=[w["signoff"].pk]),
        {"position": "objected", "comment": "", "signer_name": "Nora Neighbor"},
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "Please say what you object to." in page and 'role="alert"' in page
    assert 'value="objected" checked' in page
    assert 'value="Nora Neighbor"' in page
    with system_access("t"):
        assert not SignoffResponse.objects.exists()


def test_answer_is_recorded_and_page_thanks(client, w):
    client.force_login(w["neighbor"])
    response = client.post(
        reverse("signoff_detail", args=[w["signoff"].pk]),
        {"position": "approved", "comment": "", "signer_name": "Nora Neighbor"},
        follow=True,
    )
    page = response.content.decode()
    assert "Thank you" in page and "Back to neighbor acknowledgements" in page


def test_respond_by_and_contact_shown_when_the_board_set_them(client, w):
    page = _page(client, w)
    assert "Please respond by" not in page  # no rule, no date
    assert "Send a question to the board" in page
    community.set_rule(
        key="arc.signoff_respond_days", value="14", citation="Board resolution", actor=w["board"]
    )
    profile = CommunityProfile.get()
    profile.letter_contact = "manager@example.com, 555-0100"
    profile.save()
    page = _page(client, w)
    due = timezone.localtime(w["signoff"].sent_at).date() + datetime.timedelta(days=14)
    assert "Please respond by" in page and due.strftime("%b") in page
    assert "Contact manager@example.com, 555-0100." in page


def test_neighbor_list_says_why_they_were_asked(client, w):
    client.force_login(w["neighbor"])
    page = client.get(reverse("signoffs_mine")).content.decode()
    assert "You were asked because you live next door (left side)." in page
    assert "asked about" not in page


def test_outsider_cannot_open_or_answer(client, w):
    client.force_login(w["outsider"])
    url = reverse("signoff_detail", args=[w["signoff"].pk])
    assert client.get(url).status_code == 404
    response = client.post(url, {"position": "approved", "signer_name": "X"})
    assert response.status_code == 404


def test_expired_link_gives_next_steps(client, w):
    client.force_login(w["neighbor"])
    response = client.get(reverse("signoff_link", args=["x" * 30]))
    page = response.content.decode()
    assert response.status_code == 404
    assert reverse("signoffs_mine") in page and "Questions?" in page


def test_applicant_panel_is_simple_with_more_options(client, w):
    client.force_login(w["applicant"])
    page = client.get(reverse("arc_detail", args=[w["req"].pk])).content.decode()
    assert "Print a paper form" in page
    assert "<summary>More options</summary>" in page
    assert page.index("Print a paper form") < page.index("<summary>More options</summary>")
    assert page.index("<summary>More options</summary>") < page.index("Ask another homeowner")

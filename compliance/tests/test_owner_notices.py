"""Notices as owners see them after the UX review: plain words, one reply box,
a photo of the fix."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from community import services as community
from community.tests.factories import LotFactory, make_member
from compliance import services
from documents.attachments import attachments_for

pytestmark = pytest.mark.django_db


def png(name="fixed.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\nnot really a png", content_type="image/png")


@pytest.fixture
def w():
    lot, other = LotFactory(street_address="12 Oak Creek Drive"), LotFactory()
    board = make_member(board=True)
    community.set_rule(key="violation.cure_days", value="14", citation="Rules §2", actor=board)
    v = services.report(lot=lot, category="parking", description="Boat in driveway", actor=board)
    services.VIOLATION.apply(v, "send_notice", actor=board)
    return {
        "owner": make_member(lots=[lot]),
        "other_owner": make_member(lots=[other]),
        "board": board,
        "v": v,
    }


def test_owner_list_wording(client, w):
    client.force_login(w["other_owner"])
    page = client.get(reverse("violations")).content.decode()
    assert "Nothing to fix" in page and "You're all set" in page
    assert "Filter the list" not in page
    client.force_login(w["owner"])
    page = client.get(reverse("violations")).content.decode()
    assert "Please fix by" in page and "Cure by" not in page
    assert "V-" not in page and "12 Oak Creek Drive" in page


def test_owner_detail_wording_and_one_reply_box(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("violation_detail", args=[w["v"].pk])).content.decode()
    assert f'href="{reverse("violations")}?mine=1">Notices</a>' in page
    assert page.index('class="crumbs"') < page.index("<h1>")
    assert "Your response" in page and "formal response" in page
    assert "Please fix by" in page and "Cure by" not in page
    # While the formal response is open, Messages points to it (one reply box).
    assert "To answer this notice, use Your response above." in page
    assert 'name="body"' not in page
    services.respond(w["v"], text="Moved the boat", actor=w["owner"])
    page = client.get(reverse("violation_detail", args=[w["v"].pk])).content.decode()
    assert 'name="body"' in page and 'name="text"' not in page
    assert "For questions, use Messages below." in page


def test_owner_adds_a_photo_of_the_fix(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("violation_detail", args=[w["v"].pk])).content.decode()
    assert "Show the fix" in page
    client.post(reverse("violation_photo", args=[w["v"].pk]), {"file": [png()]})
    photo = attachments_for(w["v"]).get()
    assert photo.label == "Owner photo of the fix" and photo.uploaded_by == w["owner"]


def test_other_owner_cannot_add_a_photo(client, w):
    client.force_login(w["other_owner"])
    response = client.post(reverse("violation_photo", args=[w["v"].pk]), {"file": [png()]})
    assert response.status_code == 404
    assert not attachments_for(w["v"]).exists()


def test_no_owner_photo_once_closed(client, w):
    services.VIOLATION.apply(w["v"], "mark_cured", actor=w["board"])
    w["v"].refresh_from_db()
    client.force_login(w["owner"])
    response = client.post(reverse("violation_photo", args=[w["v"].pk]), {"file": [png()]})
    assert response.status_code == 403
    assert not attachments_for(w["v"]).exists()


def test_board_still_sees_board_wording(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("violation_detail", args=[w["v"].pk])).content.decode()
    assert f"V-{w['v'].pk}" in page and "Cure by" in page

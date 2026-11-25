"""Owner requests after the UX review: addresses, type descriptions, the form
first, input kept on errors, optional photos (scoped like ARC files)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from community import services as community
from community.models import Account, Person
from community.tests.factories import LotFactory, UserFactory, enroll_totp, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from documents.attachments import attach, attachments_for
from documents.tests.helpers import pdf
from operations import services
from operations.models import OwnerRequest

pytestmark = pytest.mark.django_db


def png(name="photo.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\nnot really a png", content_type="image/png")


@pytest.fixture
def w():
    lot, other = LotFactory(street_address="12 Oak Creek Drive"), LotFactory()
    owner = make_member(lots=[lot])
    return {
        "lot": lot,
        "owner": owner,
        "other_owner": make_member(lots=[other]),
        "board": make_member(board=True),
        "req": services.submit_request(
            lot=lot,
            request_type="maintenance",
            subject="Broken light",
            description="By the pond",
            actor=owner,
        ),
    }


def _post_new(client, data, files=None):
    return client.post(reverse("requests") + "?mine=1", {**data, **(files or {})})


def test_owner_form_first_with_address_and_type_descriptions(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("requests") + "?mine=1").content.decode()
    assert "Ask the board a question" in page
    assert page.index('id="new-request"') < page.index('aria-label="Filter"')
    assert "12 Oak Creek Drive" in page
    assert '<select name="lot"' not in page  # one home: no dropdown
    assert "Your dues, a charge or a payment on your account." in page
    assert 'type="radio" name="request_type"' in page
    assert "Filter the list" not in page  # no date filters for owners
    assert 'accept="image/*,application/pdf" multiple' in page


def test_two_homes_are_chosen_by_address(client, w):
    second = LotFactory(street_address="14 Oak Creek Drive")
    owner = make_member(lots=[w["lot"], second])
    client.force_login(owner)
    page = client.get(reverse("requests") + "?mine=1").content.decode()
    assert '<select name="lot"' in page
    assert ">14 Oak Creek Drive<" in page and ">12 Oak Creek Drive<" in page


def test_invalid_request_keeps_what_was_typed(client, w):
    client.force_login(w["owner"])
    response = _post_new(client, {"lot": w["lot"].pk, "subject": "Typed subject 4410"})
    assert response.status_code == 200
    page = response.content.decode()
    assert "Typed subject 4410" in page and "Nothing has been saved yet" in page
    assert '<details class="panel" id="new-request" open' in page


def test_new_request_with_photos(client, w):
    client.force_login(w["owner"])
    response = _post_new(
        client,
        {
            "lot": w["lot"].pk,
            "request_type": "maintenance",
            "subject": "Pothole",
            "description": "At the entrance",
        },
        {"photos": [png("hole.png"), pdf("map.pdf")]},
    )
    req = OwnerRequest.objects.unscoped("t").get(subject="Pothole")
    assert response.status_code == 302
    assert attachments_for(req).count() == 2
    page = client.get(reverse("request_detail", args=[req.pk])).content.decode()
    image = attachments_for(req).get(content_type="image/png")
    assert f'<img src="{reverse("request_file", args=[req.pk, image.pk])}"' in page
    assert f'href="{reverse("requests")}?mine=1">My requests' in page
    assert "12 Oak Creek Drive" in page


def test_requester_adds_photos_while_open(client, w):
    client.force_login(w["owner"])
    client.post(reverse("request_attach", args=[w["req"].pk]), {"photos": [png()]})
    assert attachments_for(w["req"]).count() == 1
    services.OWNER_REQUEST.apply(w["req"], "withdraw", actor=w["owner"])
    w["req"].refresh_from_db()
    response = client.post(reverse("request_attach", args=[w["req"].pk]), {"photos": [png()]})
    assert response.status_code == 403
    assert attachments_for(w["req"]).count() == 1


def test_other_owner_cannot_add_or_read_photos(client, w):
    f = attach(w["req"], png(), actor=w["owner"])
    client.force_login(w["other_owner"])
    assert (
        client.post(reverse("request_attach", args=[w["req"].pk]), {"photos": [png()]}).status_code
        == 404
    )
    assert client.get(reverse("request_file", args=[w["req"].pk, f.pk])).status_code == 404
    assert attachments_for(w["req"]).count() == 1
    client.force_login(w["owner"])
    assert client.get(reverse("request_file", args=[w["req"].pk, f.pk])).status_code == 200


def test_staff_can_add_photos(client, w):
    client.force_login(w["board"])
    client.post(reverse("request_attach", args=[w["req"].pk]), {"photos": [pdf()]})
    assert attachments_for(w["req"]).count() == 1


def test_view_only_contact_cannot_add_photos(client, w):
    user = UserFactory()
    person = community.record_person(display_name="View only", actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
        account = Account.objects.get(lot=w["lot"], closed_on__isnull=True)
    person.refresh_from_db()
    enroll_totp(user)
    community.add_account_contact(account, person=person, kind="view_only", actor=SYSTEM)
    assert not services.can_add_request_photos(user, w["req"])

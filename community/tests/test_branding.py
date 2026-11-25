"""Sign-in page background: board/sysadmin upload, hostile files refused,
hidden data stripped, served publicly (it is shown before sign-in)."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from audit.models import AuditEvent
from community import branding
from community.models import Role
from community.tests.factories import LotFactory, make_member

pytestmark = pytest.mark.django_db


def _jpeg_with_gps(size=(3000, 2000)) -> SimpleUploadedFile:
    img = Image.new("RGB", size, (40, 120, 70))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # camera make
    exif[0x010E] = "Taken at 40.6N 79.9W"  # description that gives a location away
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return SimpleUploadedFile("pond.jpg", out.getvalue(), content_type="image/jpeg")


@pytest.fixture
def board(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    }
    return make_member(board=True)


def test_board_uploads_and_the_sign_in_page_shows_it(client, board):
    client.force_login(board)
    response = client.post(reverse("branding_settings"), {"image": _jpeg_with_gps()})
    assert response.status_code == 302
    current = branding.current_login_background()
    assert current is not None
    assert max(current.width, current.height) <= branding.MAX_SIDE
    assert AuditEvent.objects.filter(action="branding.login_background_set").exists()
    client.logout()
    page = client.get(reverse("account_login"))
    url = reverse("login_background_image", args=[current.pk])
    assert url.encode() in page.content
    image = client.get(url)  # anonymous: the sign-in page needs it
    assert image.status_code == 200 and image["Content-Type"] == "image/jpeg"


def test_hidden_data_is_removed(board):
    image = branding.set_login_background(_jpeg_with_gps(), actor=board)
    with image.file.open("rb") as fh, Image.open(fh) as stored:
        assert not stored.getexif()  # no camera make, no GPS


@pytest.mark.parametrize(
    "upload",
    [
        SimpleUploadedFile("x.svg", b"<svg onload=alert(1)/>", content_type="image/svg+xml"),
        SimpleUploadedFile("x.jpg", b"<html><script>alert(1)</script>", content_type="image/jpeg"),
        SimpleUploadedFile("x.png", b"\x89PNG\r\n\x1a\nnot really a png", content_type="image/png"),
    ],
    ids=["svg", "html-named-jpg", "broken-png"],
)
def test_hostile_or_broken_files_are_refused(board, upload):
    with pytest.raises(branding.BrandingError):
        branding.set_login_background(upload, actor=board)


def test_owners_and_managers_cannot_change_it(client, board):
    for user in (make_member(lots=[LotFactory()]), make_member(roles=[Role.MANAGER])):
        client.force_login(user)
        assert client.get(reverse("branding_settings")).status_code == 403


def test_sysadmin_can_change_it(client, board):
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    assert client.get(reverse("branding_settings")).status_code == 200


def test_only_the_current_picture_is_served(client, board):
    old = branding.set_login_background(_jpeg_with_gps((800, 600)), actor=board)
    branding.set_login_background(_jpeg_with_gps((900, 600)), actor=board)
    assert client.get(reverse("login_background_image", args=[old.pk])).status_code == 404
    branding.clear_login_background(actor=board)
    assert branding.current_login_background() is None
    assert b"auth-bg" not in client.get(reverse("account_login")).content


def test_phone_picture_is_used_on_small_screens(client, board):
    desktop = branding.set_login_background(_jpeg_with_gps((1920, 1080)), actor=board)
    page = client.get(reverse("account_login")).content
    desktop_url = reverse("login_background_image", args=[desktop.pk]).encode()
    # Only a desktop picture: phones get it too.
    assert page.count(desktop_url) == 2
    phone = branding.set_login_background(
        _jpeg_with_gps((1080, 1920)), variant="phone", actor=board
    )
    page = client.get(reverse("account_login")).content
    phone_url = reverse("login_background_image", args=[phone.pk]).encode()
    assert b'media="(max-width: 700px)" srcset="' + phone_url in page
    assert b'<img src="' + desktop_url in page
    assert client.get(phone_url.decode()).status_code == 200


def test_phone_picture_alone_serves_both(client, board):
    phone = branding.set_login_background(
        _jpeg_with_gps((1080, 1920)), variant="phone", actor=board
    )
    page = client.get(reverse("account_login")).content
    assert page.count(reverse("login_background_image", args=[phone.pk]).encode()) == 2


def test_unknown_variant_is_refused(board):
    with pytest.raises(branding.BrandingError):
        branding.set_login_background(_jpeg_with_gps(), variant="tv", actor=board)

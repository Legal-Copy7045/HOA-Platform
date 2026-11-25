"""The document library as owners see it after the UX review."""

import pytest
from django.urls import reverse

from community.tests.factories import LotFactory, make_member
from documents import services
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    board = make_member(board=True)
    for title, category in (
        ("Pool rules", "policies"),
        ("June minutes", "minutes"),
        ("Declaration", "governing"),
    ):
        services.create_document(
            title=title, category=category, visibility="owners", file=pdf(), actor=board
        )
    return {"board": board, "owner": make_member(lots=[LotFactory()])}


def test_governing_documents_are_pinned_first(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("documents")).content.decode()
    assert page.index("Governing documents") < page.index("Rules and policies")
    assert page.index("Rules and policies") < page.index("Meeting minutes")
    assert "group-pinned" in page


def test_updated_date_instead_of_version_number(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("documents")).content.decode()
    assert "Updated " in page and "v1 ·" not in page


def test_records_request_link(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("documents")).content.decode()
    assert "Can't find a record?" in page and reverse("records_requests") in page


def test_breadcrumb_above_the_title_in_a_folder(client, w):
    folder = services.create_folder(name="Forms", visibility="owners", actor=w["board"])
    client.force_login(w["owner"])
    page = client.get(reverse("documents"), {"folder": folder.pk}).content.decode()
    assert page.index('class="crumbs"') < page.index("<h1>")


def test_invalid_upload_keeps_the_title(client, w):
    client.force_login(w["board"])
    response = client.post(reverse("documents"), {"title": "Typed doc 3302"})
    assert response.status_code == 200
    page = response.content.decode()
    assert "Typed doc 3302" in page and "Nothing has been saved yet" in page

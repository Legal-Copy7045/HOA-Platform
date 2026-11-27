"""A document's access history (D72) and version comparison: board and
auditor only, scoped like the document, from the audit trail without
network addresses."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from documents import services
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


def txt(body: bytes, name="rules.txt"):
    return SimpleUploadedFile(name, body, content_type="text/plain")


@pytest.fixture
def world():
    lot_a, lot_b = LotFactory(), LotFactory()
    board = make_member(board=True)
    owner_a = make_member(lots=[lot_a])
    owner_b = make_member(lots=[lot_b])
    public = services.create_document(
        title="Pool rules",
        category="policies",
        visibility="owners",
        file=txt(b"a\nb\n"),
        actor=board,
    )
    services.add_version(public, file=txt(b"a\nc\nd\n"), actor=board)
    lot_b_doc = services.create_document(
        title="Lot B survey", category="lot", visibility="lot", lot=lot_b, file=pdf(), actor=board
    )
    return {
        "board": board,
        "owner_a": owner_a,
        "owner_b": owner_b,
        "public": public,
        "lot_b_doc": lot_b_doc,
    }


def test_board_sees_who_opened_and_downloaded(client, world):
    client.force_login(world["owner_a"])
    client.get(reverse("document_detail", args=[world["public"].pk]))
    client.get(reverse("document_download", args=[world["public"].pk]))
    client.force_login(world["board"])
    response = client.get(reverse("document_access", args=[world["public"].pk]))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Downloaded version 2" in page and "Opened the page" in page
    assert "127.0.0.1" not in page
    event = AuditEvent.objects.filter(action="document.access_history_viewed").get()
    assert set(event.metadata) == {"events"}


def test_auditor_yes_manager_and_owners_no(client, world):
    url = reverse("document_access", args=[world["public"].pk])
    client.force_login(make_member(roles=[Role.AUDITOR]))
    assert client.get(url).status_code == 200
    client.force_login(make_member(roles=[Role.MANAGER]))
    assert client.get(url).status_code == 403
    client.force_login(world["owner_a"])
    assert client.get(url).status_code == 403
    # Another home's document: not found for owner A, on every new path.
    for name in ("document_access", "document_compare"):
        response = client.get(reverse(name, args=[world["lot_b_doc"].pk]))
        assert response.status_code in (403, 404)
    response = client.post(reverse("document_set_date", args=[world["lot_b_doc"].pk]), {})
    assert response.status_code in (403, 404)


def test_history_lists_only_this_document(client, world):
    client.force_login(world["owner_b"])
    client.get(reverse("document_detail", args=[world["lot_b_doc"].pk]))
    client.force_login(world["board"])
    page = client.get(reverse("document_access", args=[world["public"].pk])).content.decode()
    assert "Opened the page" not in page


def test_compare_text_versions(client, world):
    client.force_login(world["board"])
    response = client.get(reverse("document_compare", args=[world["public"].pk]))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Version 1" in page and "Version 2" in page
    assert response.context["changed"] == 2
    assert AuditEvent.objects.filter(action="document.compared").count() == 1
    # Owners see only the current version: no comparison for them.
    client.force_login(world["owner_a"])
    assert client.get(reverse("document_compare", args=[world["public"].pk])).status_code == 403


def test_compare_escapes_and_refuses_pdf(client, world):
    board = world["board"]
    doc = services.create_document(
        title="Notes",
        category="other",
        visibility="board",
        file=txt(b"<script>x</script>\n"),
        actor=board,
    )
    services.add_version(doc, file=txt(b"<b>y</b>\n"), actor=board)
    client.force_login(board)
    page = client.get(reverse("document_compare", args=[doc.pk])).content.decode()
    assert "<script>x" not in page and "&lt;script&gt;" in page
    pdf_doc = services.create_document(
        title="Scan", category="other", visibility="board", file=pdf(), actor=board
    )
    services.add_version(pdf_doc, file=pdf(), actor=board)
    assert client.get(reverse("document_compare", args=[pdf_doc.pk])).status_code == 404


def test_side_by_side():
    from documents.views import side_by_side

    rows = side_by_side(["a", "b", "c"], ["a", "x", "c", "d"])
    kinds = [r["kind"] for r in rows]
    assert kinds == ["same", "replace", "same", "insert"]
    assert rows[3]["old"] is None and rows[3]["new"] == "d"


def test_set_document_date(client, world):
    client.force_login(world["board"])
    response = client.post(
        reverse("document_set_date", args=[world["public"].pk]), {"dated_on": "2019-12-31"}
    )
    assert response.status_code == 302
    world["public"].refresh_from_db()
    assert str(world["public"].dated_on) == "2019-12-31"
    event = AuditEvent.objects.filter(action="document.dated").get()
    assert event.metadata == {"dated_on": "2019-12-31"}

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from audit.models import AuditEvent
from community.models import Ownership, Role
from community.services import end_ownership
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.scoping import system_access
from documents import services
from documents.models import Document, DocumentVersion, ScanResult, VersionScan
from documents.tests.helpers import docx, pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    lot_a, lot_b = LotFactory(), LotFactory()
    board = make_member(board=True)
    owner_a, owner_b = make_member(lots=[lot_a]), make_member(lots=[lot_b])

    def make(title, visibility="owners", lot=None):
        return services.create_document(
            title=title,
            category="governing",
            visibility=visibility,
            lot=lot,
            file=pdf(),
            actor=board,
        )

    return {
        "board": board,
        "owner_a": owner_a,
        "owner_b": owner_b,
        "lot_a": lot_a,
        "lot_b": lot_b,
        "public": make("Declaration"),
        "board_doc": make("Legal advice", "board"),
        "lot_b_doc": make("Lot B survey", "lot", lot_b),
    }


def visible(user):
    return set(Document.objects.for_user(user).values_list("title", flat=True))


def test_visibility(world):
    assert visible(world["owner_a"]) == {"Declaration"}
    assert visible(world["owner_b"]) == {"Declaration", "Lot B survey"}
    assert visible(world["board"]) == {"Declaration", "Legal advice", "Lot B survey"}
    assert visible(make_member(roles=[Role.MANAGER])) == visible(world["board"])
    assert visible(make_member(roles=[Role.SYSADMIN])) == set()  # D9


def test_former_owner_loses_lot_documents_immediately(world):
    with system_access("t"):
        ownership = Ownership.objects.get(person__user=world["owner_b"])
    end_ownership(ownership, actor=SYSTEM)
    assert visible(world["owner_b"]) == set()


def test_owner_cannot_open_or_download_others_documents(client, world):
    client.force_login(world["owner_a"])
    for doc in (world["board_doc"], world["lot_b_doc"]):
        assert client.get(reverse("document_detail", args=[doc.pk])).status_code == 404
        assert client.get(reverse("document_download", args=[doc.pk])).status_code == 404


def test_download_is_audited_and_safe(client, world):
    client.force_login(world["owner_a"])
    response = client.get(reverse("document_download", args=[world["public"].pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert "attachment" in response["Content-Disposition"]
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content).startswith(b"%PDF")
    event = AuditEvent.objects.get(action="document.downloaded")
    assert event.actor == world["owner_a"] and event.metadata == {"version": 1}


def test_versions_are_immutable_and_history_is_board_only(client, world):
    doc = world["public"]
    services.add_version(
        doc, file=pdf(body=b"%PDF-1.7 second"), note="Amended", actor=world["board"]
    )
    doc.refresh_from_db()
    assert doc.current_version.number == 2
    v1 = DocumentVersion.objects.get(document=doc, number=1)
    with pytest.raises(AppendOnlyError):
        v1.delete()
    client.force_login(world["owner_a"])
    url = reverse("document_version_download", args=[doc.pk, 1])
    assert client.get(url).status_code == 403
    client.force_login(world["board"])
    assert client.get(url).status_code == 200


@pytest.mark.parametrize(
    ("upload", "message"),
    [
        (SimpleUploadedFile("run.exe", b"MZ\x90"), "file types"),
        (SimpleUploadedFile("fake.pdf", b"MZ\x90 not a pdf"), "do not match"),
        (SimpleUploadedFile("empty.pdf", b""), "empty"),
        (SimpleUploadedFile("notes.txt", b"hi\x00there"), "binary"),
        (docx(macros=True), "macros"),
        (SimpleUploadedFile("broken.docx", b"PK\x03\x04garbage"), "do not match"),
    ],
)
def test_hostile_uploads_are_refused(world, upload, message):
    with pytest.raises(services.DocumentError, match=message):
        services.create_document(
            title="x", category="other", visibility="owners", file=upload, actor=world["board"]
        )


def test_size_limit(world, settings):
    settings.DOCUMENT_MAX_BYTES = 10
    with pytest.raises(services.DocumentError, match="MB or smaller"):
        services.create_document(
            title="x", category="other", visibility="owners", file=pdf(), actor=world["board"]
        )


def test_clean_docx_is_accepted_and_type_comes_from_the_table(world):
    doc = services.create_document(
        title="Form", category="forms", visibility="owners", file=docx(), actor=world["board"]
    )
    assert doc.current_version.content_type.endswith("wordprocessingml.document")


def test_require_clean_scan_blocks_unscanned_downloads(client, world, settings):
    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = True
    client.force_login(world["owner_a"])
    response = client.get(reverse("document_download", args=[world["public"].pk]))
    assert response.status_code == 302
    VersionScan.objects.create(
        version=world["public"].current_version, result=ScanResult.CLEAN, scanner="test"
    )
    assert client.get(reverse("document_download", args=[world["public"].pk])).status_code == 200


def test_infected_upload_is_not_stored(world, settings, monkeypatch):
    monkeypatch.setattr("documents.services.scan", lambda f: (ScanResult.INFECTED, "test"))
    with pytest.raises(services.DocumentError, match="malware"):
        services.create_document(
            title="Bad", category="other", visibility="owners", file=pdf(), actor=world["board"]
        )
    assert not Document.objects.unscoped("t").filter(title="Bad").exists()


@pytest.mark.parametrize("roles", [[Role.AUDITOR], [Role.ARC_REVIEWER]])
def test_only_board_and_manager_upload(world, roles):
    with pytest.raises(PermissionDenied):
        services.create_document(
            title="x",
            category="other",
            visibility="owners",
            file=pdf(),
            actor=make_member(roles=roles),
        )


def test_archived_documents_disappear_for_owners(world):
    services.set_archived(world["public"], archived=True, actor=world["board"])
    assert "Declaration" not in visible(world["owner_a"])
    assert "Declaration" in visible(world["board"])

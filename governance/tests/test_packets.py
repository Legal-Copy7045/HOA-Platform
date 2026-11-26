"""Meeting packets (M5): board and owner variants, attachments and the .zip."""

import io
import zipfile
from datetime import timedelta
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Person, Role
from community.tests.factories import LotFactory, make_member
from core import pdf
from core.scoping import system_access
from documents import services as documents
from documents.models import Document, DocumentVersion, ScanResult, VersionScan
from documents.tests.helpers import pdf as pdf_upload
from governance import packets
from governance import services as gov
from governance.models import MeetingPacket, MeetingPacketItem, PacketVariant

pytestmark = pytest.mark.django_db

DELINQUENT_AMOUNT = 123456  # $1,234.56


@pytest.fixture
def world():
    board = make_member(board=True)
    owner = make_member(lots=[LotFactory()])
    late_lot = LotFactory()
    late = make_member(lots=[late_lot])
    with system_access("t"):
        late_person = Person.objects.get(user=late)
    Person.objects.unscoped("t").filter(pk=late_person.pk).update(display_name="Zebulon Latepayer")
    from ledger import services as ledger

    ledger.post_charge(
        lot=late_lot,
        kind="other_charge",
        amount_cents=DELINQUENT_AMOUNT,
        description="Unpaid special charge",
        posted_on=timezone.localdate() - timedelta(days=75),
        actor=board,
    )
    ledger.open_case(lot=late_lot, notes="Zebulon owes", actor=board)
    from compliance import services as compliance

    compliance.report(lot=late_lot, category="parking", description="Zebulon trailer", actor=board)
    previous = gov.create_meeting(
        title="October board meeting",
        kind="board",
        starts_at=timezone.now() - timedelta(days=30),
        actor=board,
    )
    gov.set_meeting_status(previous, "held", actor=board)
    minutes = documents.create_document(
        title="October minutes",
        category="minutes",
        visibility="owners",
        file=pdf_upload("october.pdf"),
        actor=board,
    )
    gov.attach_minutes(previous, minutes, actor=board)
    meeting = gov.create_meeting(
        title="November board meeting",
        kind="board",
        starts_at=timezone.now() + timedelta(days=7),
        agenda="1. Call to order\n2. Pond repairs",
        actor=board,
    )
    budget_doc = documents.create_document(
        title="Pond repair quote",
        category="financial",
        visibility="owners",
        file=pdf_upload("quote.pdf"),
        actor=board,
    )
    board_doc = documents.create_document(
        title="Counsel memo",
        category="other",
        visibility="board",
        file=pdf_upload("memo.pdf"),
        actor=board,
    )
    return {
        "board": board,
        "owner": owner,
        "late_lot": late_lot,
        "meeting": meeting,
        "previous": previous,
        "minutes": minutes,
        "owner_doc": budget_doc,
        "board_doc": board_doc,
    }


def _text(data: bytes) -> str:
    return data.decode("latin-1")


def _pdf_of(packet) -> bytes:
    with packet.version.file.open("rb") as f:
        return f.read()


def _zip(client, packet):
    response = client.get(reverse("meeting_packet_zip", args=[packet.meeting_id, packet.pk]))
    assert response.status_code == 200, response.status_code
    return zipfile.ZipFile(io.BytesIO(response.content))


# --- Who may build ----------------------------------------------------------------------


def test_board_and_manager_may_build_others_may_not(world):
    manager = make_member(roles=[Role.MANAGER])
    packets.build_packet(world["meeting"], variant="board", actor=manager)
    for user in (
        world["owner"],
        make_member(roles=[Role.ARC_REVIEWER]),
        make_member(roles=[Role.AUDITOR]),
    ):
        with pytest.raises(PermissionDenied):
            packets.build_packet(world["meeting"], variant="owner", actor=user)


def test_packet_page_is_for_builders_only(client, world):
    client.force_login(world["owner"])
    assert client.get(reverse("meeting_packet", args=[world["meeting"].pk])).status_code == 403
    client.force_login(world["board"])
    response = client.get(reverse("meeting_packet", args=[world["meeting"].pk]))
    assert response.status_code == 200
    assert b"<h1>Meeting packet</h1>" in response.content


# --- Board packet -----------------------------------------------------------------------


def test_board_packet_contents_and_filing(world):
    packet = packets.build_packet(
        world["meeting"],
        variant="board",
        minutes_id=world["minutes"].pk,
        attachment_ids=[world["board_doc"].pk],
        actor=world["board"],
    )
    text = _text(_pdf_of(packet))
    for expected in (
        "Board packet",
        "CONFIDENTIAL",
        "Pond repairs",
        "Draft minutes of October board meeting",
        "Receivables aging",
        "Open items",
        "Collection cases open",
        "Attachment index",
        "Counsel memo",
    ):
        assert expected in text, expected
    # Totals only, never a name.
    assert "Zebulon" not in text
    assert packet.document.visibility == "board"
    assert packet.document.folder.name == "Meeting packets (board)"
    items = list(packet.items.all())
    assert [i.role for i in items] == ["minutes", "attachment"]
    assert all(i.included for i in items)
    # Owners can neither see the document nor its .zip.
    assert not Document.objects.for_user(world["owner"]).filter(pk=packet.document_id).exists()


def test_board_packet_is_hidden_from_owners_everywhere(client, world):
    packet = packets.build_packet(world["meeting"], variant="board", actor=world["board"])
    client.force_login(world["owner"])
    page = client.get(reverse("meeting_detail", args=[world["meeting"].pk]))
    assert page.status_code == 200
    assert b"Board packet" not in page.content
    url = reverse("meeting_packet_zip", args=[world["meeting"].pk, packet.pk])
    assert client.get(url).status_code == 404
    assert client.get(reverse("document_download", args=[packet.document_id])).status_code == 404


def test_executive_session_has_board_packet_only(world):
    exec_meeting = gov.create_meeting(
        title="Executive session",
        kind="executive",
        starts_at=timezone.now() + timedelta(days=3),
        actor=world["board"],
    )
    with pytest.raises(packets.PacketError):
        packets.build_packet(exec_meeting, variant="owner", actor=world["board"])
    packets.build_packet(exec_meeting, variant="board", actor=world["board"])


# --- Owner packet -----------------------------------------------------------------------


def test_owner_packet_has_nothing_confidential(world):
    packet = packets.build_packet(
        world["meeting"],
        variant="owner",
        attachment_ids=[world["owner_doc"].pk],
        actor=world["board"],
    )
    text = _text(_pdf_of(packet))
    assert "Owner packet" in text
    assert "Approved minutes of October board meeting" in text
    assert "Financial summary" in text
    for forbidden in (
        "Zebulon",
        "1,234.56",
        "Receivables",
        "aging",
        "Collection",
        "collection",
        "Violation",
        "violation",
        "Confidential",
        "CONFIDENTIAL",
        "Open items",
        "past due",
        "Over 90",
        "90 days",
    ):
        assert forbidden not in text, forbidden
    assert packet.document.visibility == "owners"
    assert packet.document.folder.name == "Meeting packets"


def test_owner_packet_accepts_only_owner_visible_attachments(world):
    with pytest.raises(packets.PacketError):
        packets.build_packet(
            world["meeting"],
            variant="owner",
            attachment_ids=[world["board_doc"].pk],
            actor=world["board"],
        )
    assert not MeetingPacket.objects.exists()


def test_owners_open_the_owner_packet_from_the_meeting_page(client, world):
    packet = packets.build_packet(
        world["meeting"],
        variant="owner",
        attachment_ids=[world["owner_doc"].pk],
        actor=world["board"],
    )
    client.force_login(world["owner"])
    page = client.get(reverse("meeting_detail", args=[world["meeting"].pk]))
    assert b"Owner packet" in page.content
    assert reverse("meeting_packet_zip", args=[world["meeting"].pk, packet.pk]).encode() in (
        page.content
    )
    assert b"Build packet" not in page.content
    assert client.get(reverse("document_download", args=[packet.document_id])).status_code == 200
    names = _zip(client, packet).namelist()
    assert names[0].startswith("00-owner-packet-") and names[0].endswith(".pdf")
    assert names[1:] == ["01-october.pdf", "02-quote.pdf"]


def test_owners_get_only_the_latest_owner_packet(client, world):
    first = packets.build_packet(world["meeting"], variant="owner", actor=world["board"])
    second = packets.build_packet(world["meeting"], variant="owner", actor=world["board"])
    assert first.document_id == second.document_id
    assert second.version.number == first.version.number + 1
    assert DocumentVersion.objects.filter(document=first.document).count() == 2
    client.force_login(world["owner"])
    old = reverse("meeting_packet_zip", args=[world["meeting"].pk, first.pk])
    assert client.get(old).status_code == 404
    _zip(client, second)
    client.force_login(world["board"])
    _zip(client, first)  # the board keeps every version


# --- Attachments and the .zip -----------------------------------------------------------


def test_zip_skips_files_that_failed_or_await_scanning(client, world, settings):
    board = world["board"]
    pending = documents.create_document(
        title="Pending file",
        category="other",
        visibility="board",
        file=pdf_upload("p.pdf"),
        actor=board,
    )  # unscanned
    infected = documents.create_document(
        title="Infected file",
        category="other",
        visibility="board",
        file=pdf_upload("i.pdf"),
        actor=board,
    )
    errored = documents.create_document(
        title="Scanner error",
        category="other",
        visibility="board",
        file=pdf_upload("e.pdf"),
        actor=board,
    )
    with mock.patch("documents.services.scan", return_value=(ScanResult.CLEAN, "test")):
        clean = documents.create_document(
            title="Clean file",
            category="other",
            visibility="board",
            file=pdf_upload("c.pdf"),
            actor=board,
        )
    VersionScan.objects.create(
        version=infected.current_version, result=ScanResult.INFECTED, scanner="test"
    )
    VersionScan.objects.create(
        version=errored.current_version, result=ScanResult.ERROR, scanner="test"
    )
    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = True
    with mock.patch("documents.services.scan", return_value=(ScanResult.CLEAN, "test")):
        packet = packets.build_packet(
            world["meeting"],
            variant="board",
            attachment_ids=[pending.pk, infected.pk, errored.pk, clean.pk],
            actor=board,
        )
    items = {i.document_id: i for i in packet.items.all()}
    assert items[clean.pk].included
    assert items[pending.pk].skipped_reason == "waiting for a clean malware scan"
    assert items[infected.pk].skipped_reason == "did not pass the malware scan"
    assert items[errored.pk].skipped_reason == "did not pass the malware scan"
    text = _text(_pdf_of(packet))
    assert "Not included: waiting for a clean malware scan." in text
    assert "Not included: did not pass the malware scan." in text
    client.force_login(board)
    archive = _zip(client, packet)
    names = archive.namelist()
    assert [n for n in names if n.endswith("c.pdf")] and "NOT-INCLUDED.txt" in names
    assert not [n for n in names if n.endswith(("p.pdf", "i.pdf", "e.pdf"))]
    # A file rescanned as infected after the build is dropped from the .zip too.
    VersionScan.objects.create(
        version=clean.current_version, result=ScanResult.INFECTED, scanner="test"
    )
    names = _zip(client, packet).namelist()
    assert not [n for n in names if n.endswith("c.pdf")]


def test_zip_entry_names_are_sanitized(client, world):
    hostile = documents.create_document(
        title="Hostile name",
        category="other",
        visibility="board",
        file=pdf_upload("../../etc/..\\evil name.hta.pdf"),
        actor=world["board"],
    )
    packet = packets.build_packet(
        world["meeting"], variant="board", attachment_ids=[hostile.pk], actor=world["board"]
    )
    client.force_login(world["board"])
    for name in _zip(client, packet).namelist():
        assert "/" not in name and "\\" not in name and ".." not in name
        assert name.endswith((".pdf", ".txt"))
    # Whatever is stored, the entry ends in the extension of the validated type.
    forged = DocumentVersion(original_filename="../../run.exe", content_type="application/pdf")
    assert packets._zip_name(3, forged) == "03-run.pdf"


def test_attachments_are_never_parsed(world):
    with mock.patch("zipfile.ZipFile.__init__", side_effect=AssertionError("parsed")):
        packets.build_packet(
            world["meeting"],
            variant="board",
            attachment_ids=[world["board_doc"].pk],
            actor=world["board"],
        )


# --- Hostile text -----------------------------------------------------------------------


def test_pdf_escapes_hostile_text(world):
    meeting = gov.create_meeting(
        title="Evil) Tj ET BT /F2 99 Tf (x",
        kind="board",
        starts_at=timezone.now() + timedelta(days=9),
        agenda="Back\\slash ) Tj **bold** \N{RIGHT-TO-LEFT OVERRIDE} endobj\n%%EOF",
        actor=world["board"],
    )
    data = _pdf_of(packets.build_packet(meeting, variant="board", actor=world["board"]))
    assert b"Evil\\) Tj ET BT /F2 99 Tf \\(x" in data
    assert b"(Evil) Tj" not in data
    assert b"Back\\\\slash \\) Tj" in data
    assert b"* *bold* *" in data and b"?" in data
    # Inside a string it is only text; the file still has one real trailer.
    assert b"(%%EOF) Tj" in data and data.rstrip().endswith(b"\n%%EOF")
    assert data.count(b"startxref") == 1


def test_plain_never_leaves_bold_markers():
    assert "**" not in pdf.plain("a***b****c")


# --- Audit ------------------------------------------------------------------------------


def test_build_and_zip_download_are_audited(client, world):
    packet = packets.build_packet(
        world["meeting"],
        variant="owner",
        attachment_ids=[world["owner_doc"].pk],
        actor=world["board"],
    )
    built = AuditEvent.objects.filter(action="meeting.packet_built").get()
    assert built.metadata == {
        "variant": "owner",
        "document_id": packet.document_id,
        "version": 1,
        "attachments": 2,
        "skipped": 0,
    }
    client.force_login(world["owner"])
    _zip(client, packet)
    event = AuditEvent.objects.filter(action="meeting.packet_zip_downloaded").get()
    assert event.metadata["files"] == 3 and event.metadata["skipped"] == 0
    assert AuditEvent.objects.filter(action="document.downloaded").count() == 2


def test_packet_records_are_append_only(world):
    from core.append_only import AppendOnlyError

    packet = packets.build_packet(world["meeting"], variant="board", actor=world["board"])
    with pytest.raises(AppendOnlyError):
        packet.save()
    with pytest.raises(AppendOnlyError):
        MeetingPacketItem.objects.all().delete()


# --- The page ---------------------------------------------------------------------------


def test_build_from_the_page(client, world):
    client.force_login(world["board"])
    url = reverse("meeting_packet", args=[world["meeting"].pk])
    response = client.post(url, {"variant": "owner", "owner-attachments": [world["owner_doc"].pk]})
    assert response.status_code == 302
    assert MeetingPacket.objects.get().variant == PacketVariant.OWNER


def test_page_refusal_shows_again_on_the_form(client, world):
    client.force_login(world["board"])
    url = reverse("meeting_packet", args=[world["meeting"].pk])
    response = client.post(url, {"variant": "owner", "owner-attachments": [world["board_doc"].pk]})
    assert response.status_code == 200
    assert b'role="alert"' in response.content or b"errorlist" in response.content
    assert not MeetingPacket.objects.exists()

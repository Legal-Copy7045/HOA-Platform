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
    gov.approve_minutes(previous, actor=board)
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
    assert response.streaming  # never built in memory (SEC-002)
    return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))


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


def test_zip_is_streamed_with_bounded_memory(client, world, monkeypatch):
    """SEC-002: the .zip goes out chunk by chunk; the builder never holds the
    whole archive (each piece and the sink's peak stay near one chunk)."""
    import os

    monkeypatch.setattr(packets, "ZIP_CHUNK_BYTES", 16 * 1024)
    big = [
        documents.create_document(
            title=f"Scan {n}",
            category="other",
            visibility="owners",
            file=pdf_upload(f"scan{n}.pdf", b"%PDF-1.7\n" + os.urandom(300 * 1024) + b"\n%%EOF\n"),
            actor=world["board"],
        )
        for n in range(3)
    ]
    packet = packets.build_packet(
        world["meeting"],
        variant="owner",
        attachment_ids=[d.pk for d in big],
        actor=world["board"],
    )
    seen = {}
    real = packets.PacketZipStream.__init__

    def spy(self, *args, **kwargs):
        real(self, *args, **kwargs)
        seen["stream"] = self

    monkeypatch.setattr(packets.PacketZipStream, "__init__", spy)
    client.force_login(world["owner"])
    response = client.get(reverse("meeting_packet_zip", args=[world["meeting"].pk, packet.pk]))
    from django.http import StreamingHttpResponse

    assert isinstance(response, StreamingHttpResponse)
    assert not hasattr(response, "content") or response.streaming
    pieces = list(response.streaming_content)
    total = sum(len(p) for p in pieces)
    assert total > 900 * 1024 and len(pieces) > 40
    limit = 64 * 1024  # one 16 KiB chunk plus zip headers and compressor output
    assert max(len(p) for p in pieces) < limit
    assert seen["stream"].sink.most_held < limit
    archive = zipfile.ZipFile(io.BytesIO(b"".join(pieces)))
    assert archive.testzip() is None
    assert len([n for n in archive.namelist() if n.endswith("scan0.pdf")]) == 1


def test_zip_downloads_are_rate_limited_per_person(client, world, monkeypatch):
    packet = packets.build_packet(world["meeting"], variant="owner", actor=world["board"])
    url = reverse("meeting_packet_zip", args=[world["meeting"].pk, packet.pk])
    client.force_login(world["owner"])

    # One at a time: while a download is still streaming, a second is refused.
    first = client.get(url)
    assert first.status_code == 200
    second = client.get(url)
    assert second.status_code == 302 and second["Location"].endswith(
        reverse("meeting_detail", args=[world["meeting"].pk])
    )
    # Someone else is not held up by this person's download.
    client2 = type(client)()
    client2.force_login(world["board"])
    other = client2.get(url)
    assert other.status_code == 200
    b"".join(other.streaming_content)
    # Finishing the first frees the slot (the test client closes it at the end).
    b"".join(first.streaming_content)
    again = client.get(url)
    assert again.status_code == 200
    b"".join(again.streaming_content)

    # A window cap, counted from the audit log across processes.
    monkeypatch.setattr(packets, "ZIP_RATE_LIMIT", 3)
    third = client.get(url)
    b"".join(third.streaming_content)
    mine = AuditEvent.objects.filter(action="meeting.packet_zip_downloaded", actor=world["owner"])
    assert mine.count() == 3
    refused = client.get(url, follow=True)
    assert b"several packets in the last few minutes" in refused.content
    assert mine.count() == 3  # a refused download is not a download


def test_zip_slot_is_freed_even_if_never_read(world):
    packet = packets.build_packet(world["meeting"], variant="owner", actor=world["board"])
    stream, _ = packets.packet_zip(packet, user=world["owner"])
    with pytest.raises(packets.PacketBusy):
        packets.packet_zip(packet, user=world["owner"])
    stream.close()  # what Django does when the response ends
    stream2, _ = packets.packet_zip(packet, user=world["owner"])
    stream2.close()


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


# --- Minutes approval (SEC-003) ---------------------------------------------------------


def _held_meeting_with_minutes(world, actor, title="December board meeting"):
    held = gov.create_meeting(
        title=title,
        kind="board",
        starts_at=timezone.now() - timedelta(days=3),
        actor=world["board"],
    )
    gov.set_meeting_status(held, "held", actor=world["board"])
    doc = gov.upload_minutes(held, file=pdf_upload("december.pdf"), actor=actor)
    held.refresh_from_db()
    return held, doc


def _next_meeting(world):
    return gov.create_meeting(
        title="January board meeting",
        kind="board",
        starts_at=timezone.now() + timedelta(days=20),
        actor=world["board"],
    )


def test_manager_attaches_drafts_but_only_the_board_approves(client, world):
    manager = make_member(roles=[Role.MANAGER])
    held, doc = _held_meeting_with_minutes(world, manager)
    assert held.minutes_document_id == doc.pk  # data entry is allowed
    assert gov.minutes_approval(held) is None
    nxt = _next_meeting(world)
    # Draft minutes are never presented to owners as approved.
    assert packets.approved_minutes(nxt) is None
    text = _text(_pdf_of(packets.build_packet(nxt, variant="owner", actor=manager)))
    assert "Approved minutes" not in text
    assert "No approved minutes" in text

    for user in (manager, world["owner"], make_member(roles=[Role.AUDITOR])):
        with pytest.raises(PermissionDenied):
            gov.approve_minutes(held, actor=user)
    client.force_login(manager)
    url = reverse("meeting_action", args=[held.pk, "approve_minutes"])
    page = client.get(reverse("meeting_detail", args=[held.pk]))
    assert b"Draft minutes, not yet approved" in page.content
    assert b"Mark minutes approved" not in page.content
    assert client.post(url, {"approved_on": timezone.localdate().isoformat()}).status_code == 403
    client.force_login(world["owner"])
    assert client.post(url, {"approved_on": timezone.localdate().isoformat()}).status_code == 403
    assert not AuditEvent.objects.filter(action="meeting.minutes_approved", target_id=str(held.pk))

    # The board marks them approved on the page: recorded, audited, then shown.
    client.force_login(world["board"])
    page = client.get(reverse("meeting_detail", args=[held.pk]))
    assert b"Mark minutes approved" in page.content
    response = client.post(
        url,
        {
            "approved_on": timezone.localdate().isoformat(),
            "note": "At the January meeting",
            "version": doc.current_version_id,
        },
    )
    assert response.status_code == 302
    approval = gov.minutes_approval(held)
    assert approval.version_id == doc.current_version_id
    assert approval.recorded_by == world["board"] and approval.note == "At the January meeting"
    event = AuditEvent.objects.filter(action="meeting.minutes_approved").latest("occurred_at")
    assert event.metadata == {"document_id": doc.pk, "version": 1}
    assert event.actor == world["board"] and event.target_id == str(held.pk)
    assert packets.approved_minutes(nxt) == doc
    text = _text(_pdf_of(packets.build_packet(nxt, variant="owner", actor=manager)))
    assert "Approved minutes of December board meeting" in text
    assert "approved by the board on" in text
    client.force_login(world["owner"])
    assert b"Minutes approved" in client.get(reverse("meeting_detail", args=[held.pk])).content


def test_minutes_approval_rules_and_records(client, world):
    from core.append_only import AppendOnlyError
    from governance.models import MinutesApproval

    held, doc = _held_meeting_with_minutes(world, world["board"])
    today = timezone.localdate()
    with pytest.raises(gov.GovernanceError):
        gov.approve_minutes(held, approved_on=today + timedelta(days=1), actor=world["board"])
    with pytest.raises(gov.GovernanceError):
        gov.approve_minutes(held, approved_on=today - timedelta(days=30), actor=world["board"])
    with pytest.raises(gov.GovernanceError):
        gov.approve_minutes(_next_meeting(world), actor=world["board"])  # nothing attached
    approval = gov.approve_minutes(held, actor=world["board"])
    with pytest.raises(gov.GovernanceError):
        gov.approve_minutes(held, actor=world["board"])  # once per version
    with pytest.raises(AppendOnlyError):
        approval.save()
    with pytest.raises(AppendOnlyError):
        MinutesApproval.objects.all().delete()

    # A corrected version is a draft again until the board approves it.
    documents.add_version(doc, file=pdf_upload("december-v2.pdf"), note="Fix", actor=world["board"])
    held.refresh_from_db()
    assert gov.minutes_approval(held) is None
    assert packets.approved_minutes(_next_meeting(world)) is None
    gov.approve_minutes(held, actor=world["board"])
    assert gov.minutes_approval(held).version.number == 2

    # A refused form comes back with what was typed.
    other, other_doc = _held_meeting_with_minutes(
        world, world["board"], title="Special board meeting"
    )
    client.force_login(world["board"])
    page = client.post(
        reverse("meeting_action", args=[other.pk, "approve_minutes"]),
        {
            "approved_on": "2001-01-01",
            "note": "Keep this note",
            "version": other_doc.current_version_id,
        },
    )
    assert page.status_code == 200
    assert b"before the meeting" in page.content and b"Keep this note" in page.content
    assert gov.minutes_approval(other) is None


def test_runtime_role_cannot_change_minutes_approvals(world):
    from django.db import DatabaseError, connection, transaction

    held, _ = _held_meeting_with_minutes(world, world["board"])
    approval = gov.approve_minutes(held, actor=world["board"])
    for sql in (
        "UPDATE governance_minutesapproval SET note = 'x' WHERE id = %s",
        "DELETE FROM governance_minutesapproval WHERE id = %s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [approval.pk])


# --- fixres BUG-006 / SEC-001 / R2-001: the version the board member read ----------------------


def test_minutes_approval_is_for_the_version_on_the_page(client, world):
    held, doc = _held_meeting_with_minutes(world, world["board"])
    client.force_login(world["board"])
    page = client.get(reverse("meeting_detail", args=[held.pk]))
    form = page.context["approve_form"]
    seen = form["version"].value()
    assert seen == doc.current_version_id
    assert b"You are approving version 1" in page.content
    assert b"Mark version 1 of these minutes approved" in page.content
    # The manager publishes a revised file while the page is open.
    documents.add_version(
        doc, file=pdf_upload("december-v2.pdf"), note="Edit", actor=world["board"]
    )
    url = reverse("meeting_action", args=[held.pk, "approve_minutes"])
    today = timezone.localdate().isoformat()
    page = client.post(url, {"approved_on": today, "note": "As read", "version": seen})
    assert page.status_code == 200  # the form comes back, nothing recorded
    assert b"A new version of the minutes (version 2)" in page.content
    assert b"As read" in page.content
    # The form now offers the new version, which the page names.
    doc.refresh_from_db()
    assert page.context["approve_form"]["version"].value() == str(doc.current_version_id)
    assert b"You are approving version 2" in page.content
    assert gov.minutes_approval(held) is None
    doc.refresh_from_db()
    client.post(url, {"approved_on": today, "version": doc.current_version_id})
    assert gov.minutes_approval(held).version.number == 2
    # Without the version the form is refused.
    other, _ = _held_meeting_with_minutes(world, world["board"], title="Special board meeting")
    page = client.post(
        reverse("meeting_action", args=[other.pk, "approve_minutes"]), {"approved_on": today}
    )
    assert page.status_code == 200 and gov.minutes_approval(other) is None
    with pytest.raises(gov.GovernanceError):
        gov.approve_minutes(other, version_id=doc.current_version_id, actor=world["board"])

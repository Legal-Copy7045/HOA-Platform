"""Files waiting for a malware scan (external review 2026-11-26, D57): the
retry after upload, the 10-minute sweep, the System status list (operations
data only), "Scan waiting files now" and the health check."""

import datetime
import socket
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import make_member
from core import health
from core.scoping import system_access
from documents import scan_queue, scanning
from documents import services as docs
from documents.models import (
    Attachment,
    AttachmentScan,
    Document,
    DocumentVersion,
    ScanResult,
    VersionScan,
)
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db

URL = reverse("system_status")
PRIVATE_TITLE = "Smith family hardship letter"  # never shown to the sysadmin
PRIVATE_FILENAME = "smith-hardship.pdf"


@pytest.fixture
def clamd(settings):
    settings.DOCUMENT_SCANNER = "clamd"
    settings.DOCUMENT_CLAMD_ADDRESS = "clamav:3310"
    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = True
    return settings


def _scanner(result, detail=""):
    return mock.patch(
        "documents.scan_queue.scan", return_value=scanning.ScanOutcome(result, "clamd", detail)
    )


def _document(board=None):
    """A document uploaded now whose scan found the scanner unreachable."""
    with mock.patch(
        "documents.services.scan",
        return_value=scanning.ScanOutcome(ScanResult.ERROR, "clamd", scanning.UNREACHABLE),
    ):
        doc = docs.create_document(
            title=PRIVATE_TITLE,
            category="governing",
            visibility="board",
            actor=board or make_member(board=True),
            file=pdf(PRIVATE_FILENAME),
        )
    return doc.versions.get()


def _aged_version(minutes_ago, detail=scanning.UNREACHABLE):
    """A stored version uploaded (and first scanned) ``minutes_ago``. Records
    are append-only, so the times are set when the rows are created."""
    when = timezone.now() - datetime.timedelta(minutes=minutes_ago)
    user = make_member(board=True)
    with system_access("t"):
        doc = Document.objects.create(
            title=PRIVATE_TITLE, category="governing", visibility="board", created_by=user
        )
        version = DocumentVersion(
            document=doc,
            number=1,
            original_filename=PRIVATE_FILENAME,
            content_type="application/pdf",
            size=2048,
            sha256="0" * 64,
            uploaded_by=user,
            uploaded_at=when,
        )
        version.file.save("upload", ContentFile(b"%PDF-1.4\n%%EOF\n"), save=False)
        version.save()
        VersionScan.objects.create(
            version=version,
            result=ScanResult.ERROR,
            scanner="clamd",
            detail=detail,
            scanned_at=when,
        )
    return version


def _attachment(owner_type="operations.ownerrequest", result=ScanResult.ERROR, detail="", **kw):
    att = Attachment(
        owner_type=owner_type,
        owner_id="1",
        original_filename=PRIVATE_FILENAME,
        content_type="application/pdf",
        size=2048,
        sha256="0" * 64,
        scan_result=result,
        scan_detail=detail,
        uploaded_by=make_member(board=True),
        **kw,
    )
    att.file.save("upload", ContentFile(b"%PDF-1.4\n%%EOF\n"), save=False)
    att.save()
    return att


# --- why files were stuck, and the retries ----------------------------------------------


def test_reasons_are_kept_in_plain_words(clamd):
    with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError):
        assert scanning.scan(pdf()).detail == scanning.UNREACHABLE
    with mock.patch("socket.create_connection", side_effect=socket.gaierror):
        assert scanning.scan(pdf()).detail == scanning.UNREACHABLE
    with mock.patch("socket.create_connection", side_effect=TimeoutError):
        outcome = scanning.scan(pdf())
    assert tuple(outcome) == (ScanResult.ERROR, "clamd") and outcome.detail == scanning.TIMEOUT
    clamd.DOCUMENT_SCANNER = "none"
    assert scanning.scan(pdf()).detail == scanning.NOT_CONFIGURED


def test_an_upload_the_scanner_missed_is_retried_after_commit(
    clamd, django_capture_on_commit_callbacks
):
    with (
        mock.patch("documents.tasks.scan_file.configure") as configure,
        django_capture_on_commit_callbacks(execute=True),
    ):
        version = _document()
    configure.assert_called_once_with(
        queueing_lock=f"scan-version-{version.pk}",
        schedule_in={"seconds": scan_queue.RETRY_AFTER_UPLOAD_SECONDS},
    )
    configure.return_value.defer.assert_called_once_with(kind="version", pk=version.pk)
    assert version.scans.get().detail == scanning.UNREACHABLE


def test_attachment_uploads_are_retried_too(clamd, django_capture_on_commit_callbacks):
    from documents import attachments

    owner = make_member(board=True)
    with (
        mock.patch(
            "documents.attachments.scan",
            return_value=scanning.ScanOutcome(ScanResult.ERROR, "clamd", scanning.TIMEOUT),
        ),
        mock.patch("documents.tasks.scan_file.configure") as configure,
        django_capture_on_commit_callbacks(execute=True),
    ):
        att = attachments.attach(owner, pdf(), actor=owner)
    assert att.scan_detail == scanning.TIMEOUT
    configure.return_value.defer.assert_called_once_with(kind="attachment", pk=att.pk)


def test_clean_uploads_queue_nothing(clamd, django_capture_on_commit_callbacks):
    with (
        mock.patch(
            "documents.services.scan", return_value=scanning.ScanOutcome(ScanResult.CLEAN, "clamd")
        ),
        mock.patch("documents.tasks.scan_file.configure") as configure,
        django_capture_on_commit_callbacks(execute=True),
    ):
        docs.create_document(
            title="Rules", category="governing", visibility="owners",
            actor=make_member(board=True), file=pdf(),
        )  # fmt: skip
    configure.assert_not_called()


def test_rescan_adds_a_scan_row_and_releases_the_file(clamd):
    version = _document()
    assert not docs.downloadable(version)
    with _scanner(ScanResult.CLEAN):
        assert scan_queue.rescan("version", version.pk) == ScanResult.CLEAN
    assert docs.downloadable(version)
    assert version.scans.count() == 2  # append-only: the failed scan stays
    # Not waiting any more: a second job does nothing.
    with _scanner(ScanResult.CLEAN) as scan:
        assert scan_queue.rescan("version", version.pk) is None
    scan.assert_not_called()
    with pytest.raises(ValueError):
        scan_queue.rescan("document", version.pk)


def test_rescan_of_an_attachment_and_the_task(clamd):
    from documents.tasks import scan_file

    att = _attachment()
    with _scanner(ScanResult.ERROR, scanning.UNREACHABLE):
        assert scan_file(kind="attachment", pk=att.pk) == ScanResult.ERROR
    assert AttachmentScan.objects.get(attachment=att).detail == scanning.UNREACHABLE
    with _scanner(ScanResult.CLEAN):
        assert scan_file(kind="attachment", pk=att.pk) == ScanResult.CLEAN
    assert scan_file(kind="attachment", pk=att.pk) == "not waiting"


def test_the_sweep_waits_for_the_scanner_to_answer(clamd):
    from documents.tasks import scan_sweep

    version = _document()
    with mock.patch("core.health.scanner_version", return_value={"answering": False}):
        assert scan_sweep(timestamp=0) == 0
    assert version.scans.count() == 1  # no rows pile up during an outage
    with (
        mock.patch("core.health.scanner_version", return_value={"answering": True}),
        _scanner(ScanResult.CLEAN),
    ):
        assert scan_sweep(timestamp=0) == 1
    assert scan_queue.waiting_files() == []


def test_the_sweep_does_nothing_with_scanning_off(settings):
    settings.DOCUMENT_SCANNER = "none"
    _attachment(result=ScanResult.UNSCANNED, detail=scanning.NOT_CONFIGURED)
    with mock.patch("core.health.scanner_version") as version:
        assert scan_queue.sweep() == 0
    version.assert_not_called()


def test_the_sweep_runs_every_ten_minutes():
    from procrastinate.contrib.django import app

    assert "documents.tasks.scan_file" in app.tasks  # autodiscovered by the worker
    crons = {
        name: task.cron
        for (name, _), task in app.periodic_registry.periodic_tasks.items()
        if name == "documents.tasks.scan_sweep"
    }
    assert crons == {"documents.tasks.scan_sweep": "*/10 * * * *"}


# --- what the list says -----------------------------------------------------------------


def test_waiting_list_reads_in_plain_words(clamd):
    _aged_version(20)
    _attachment()  # owner request photo, unreachable (old row: no reason kept)
    _attachment("arc.arcrequest", detail=scanning.TIMEOUT, label="Signed acknowledgement form")
    _attachment("compliance.violation", result=ScanResult.UNSCANNED)
    _attachment("arc.arcrequest", result=ScanResult.CLEAN)  # not waiting
    files = scan_queue.waiting_files()
    assert [f.area for f in files] == [
        "Document library",
        "Owner request photo",
        "ARC neighbor acknowledgement form",
        "Violation photo",
    ]
    doc, photo, form, violation = files
    assert doc.stuck and doc.waited_minutes >= 20 and doc.attempts == 1
    assert doc.last_result == "Scanner unreachable"
    assert photo.last_result == "Scanner error" and not photo.stuck
    assert form.last_result == "Timed out"
    assert violation.last_result == "Never attempted (scanning was off)"
    assert violation.attempts == 0 and violation.last_attempt_at is None
    assert scan_queue.waiting_count() == 4


def test_the_wait_restarts_when_a_rescan_fails_after_a_clean_scan(clamd):
    version = _aged_version(600)
    VersionScan.objects.create(
        version=version,
        result=ScanResult.CLEAN,
        scanner="clamd",
        scanned_at=timezone.now() - datetime.timedelta(minutes=500),
    )
    VersionScan.objects.create(
        version=version,
        result=ScanResult.ERROR,
        scanner="clamd",
        detail=scanning.TIMEOUT,
        scanned_at=timezone.now() - datetime.timedelta(minutes=5),
    )
    (item,) = scan_queue.waiting_files()
    assert item.attempts == 3 and 4 <= item.waited_minutes <= 6 and not item.stuck


def test_status_page_lists_waiting_files_without_names_or_owners(client, clamd):
    _aged_version(40)
    _attachment()
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(URL).content.decode()
    section = page.split('id="scan-queue"', 1)[1].split("</section>", 1)[0]
    assert "Waiting for a scan" in section
    assert "Document library" in section and "Owner request photo" in section
    assert "Stuck" in section and "Scanner unreachable" in section
    assert "2.0\xa0KB" in section or "2.0 KB" in section
    assert "Scan waiting files now" in section
    assert PRIVATE_TITLE not in page and PRIVATE_FILENAME not in page
    assert 'href="#scan-queue"' in page


def test_status_page_says_when_nothing_waits(client, clamd):
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    section = client.get(URL).content.decode().split('id="scan-queue"', 1)[1]
    assert "No files are waiting" in section.split("</section>", 1)[0]
    assert "Scan waiting files now" not in section.split("</section>", 1)[0]


def test_scan_waiting_files_now_queues_each_file_and_is_audited(client, clamd):
    version = _document()
    att = _attachment()
    admin = make_member(roles=[Role.SYSADMIN])
    client.force_login(admin)
    with mock.patch("documents.tasks.scan_file.configure") as configure:
        response = client.post(URL, {"action": "scan_waiting"})
    assert response.status_code == 302 and response["Location"].endswith("#scan-queue")
    locks = sorted(c.kwargs["queueing_lock"] for c in configure.call_args_list)
    assert locks == sorted([f"scan-version-{version.pk}", f"scan-attachment-{att.pk}"])
    event = AuditEvent.objects.get(action="system.scan_waiting_queued")
    assert event.metadata == {"waiting": 2, "queued": 2}
    assert event.actor == admin


def test_files_already_queued_are_not_queued_twice(clamd):
    from procrastinate.exceptions import AlreadyEnqueued

    _document()
    admin = make_member(roles=[Role.SYSADMIN])
    with mock.patch("documents.tasks.scan_file.configure") as configure:
        configure.return_value.defer.side_effect = AlreadyEnqueued("queued")
        assert scan_queue.queue_waiting(actor=admin) == 0


def test_only_sysadmins_can_queue_scans(client, clamd):
    from django.core.exceptions import PermissionDenied

    _document()
    board = make_member(board=True)
    with pytest.raises(PermissionDenied):
        scan_queue.queue_waiting(actor=board)
    client.force_login(board)
    assert client.post(URL, {"action": "scan_waiting"}).status_code == 403
    assert not AuditEvent.objects.filter(action="system.scan_waiting_queued").exists()


# --- the health check -------------------------------------------------------------------


def test_health_check_fails_after_thirty_minutes(clamd):
    assert health._scan_queue() == (True, "None waiting")
    _aged_version(10)
    ok, detail = health._scan_queue()
    assert ok and detail.startswith("1 file waiting, the oldest for 10 minutes")
    _aged_version(31)
    ok, detail = health._scan_queue()
    assert not ok
    assert detail.startswith("2 files cannot be downloaded until scanned; the oldest has waited 31")
    assert "Scan waiting files now" in detail
    assert PRIVATE_TITLE not in detail and PRIVATE_FILENAME not in detail


def test_health_check_is_hidden_while_scanning_is_off(settings):
    settings.DOCUMENT_SCANNER = "none"
    assert health._scan_queue() is None
    assert "scan_queue" not in {r.key for r in health.run_checks()}

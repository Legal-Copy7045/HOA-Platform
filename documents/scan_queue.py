"""Files waiting for a malware scan (external review 2026-11-26, D57).

Every upload is scanned while it is stored. When the scanner gives no answer
(ClamAV still loading its signatures after a restart, the container down, a
time-out) the file is kept but cannot be downloaded, and it waits here:

* ``queue_soon`` asks the worker to try that file again a couple of minutes
  after the upload commits;
* the worker's ``documents.tasks.scan_sweep`` (every 10 minutes) scans every
  waiting file again once the scanner answers;
* System status lists the waiting files and has "Scan waiting files now"
  (``queue_waiting``, sysadmin, audited with a count only).

What this module reports is operations data only: where a file belongs in
plain words, when it was uploaded, its size and how its scans went. Never a
file name, title, label text, owner or address (sysadmins get no owner data,
CLAUDE.md), so the same list is safe on the sysadmin's page.

Before this, a file whose upload-time scan failed stayed blocked until someone
ran ``rescan_files`` by hand: nothing ever tried again (the "2 waiting" the
owner could not explain).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from core.scoping import system_access
from documents.models import Attachment, AttachmentScan, DocumentVersion, ScanResult, VersionScan
from documents.scanning import DETAIL_WORDS, detail_of, scan

log = logging.getLogger(__name__)

VERSION = "version"
ATTACHMENT = "attachment"
KINDS = (VERSION, ATTACHMENT)
# Latest results that keep a file waiting (the download gate refuses both
# when clean scans are required; see documents.services.scan_allows_download).
BLOCKED = (ScanResult.ERROR, ScanResult.UNSCANNED)
RETRY_AFTER_UPLOAD_SECONDS = 120
SWEEP_LIMIT = 50  # files per sweep, so one run never holds the worker long

# Where a file belongs, in plain words, by the model it is attached to.
AREAS = {
    "arc.arcrequest": "ARC request attachment",
    "compliance.violation": "Violation photo",
    "operations.ownerrequest": "Owner request photo",
    "operations.invoice": "Invoice",
    "operations.workorder": "Work order attachment",
    "community.accountchangerequest": "Recorded deed (account change)",
    "ledger.resalerequest": "Resale certificate",
    "governance.recordsrequest": "Records request file",
}
ARC_FORM_LABELS = ("Signed acknowledgement form", "Signed sign-off form")


@dataclass(frozen=True)
class WaitingFile:
    kind: str
    pk: int
    area: str
    uploaded_at: datetime.datetime
    waiting_since: datetime.datetime
    size: int
    attempts: int
    last_result: str
    last_attempt_at: datetime.datetime | None

    @property
    def waited(self) -> datetime.timedelta:
        return timezone.now() - self.waiting_since

    @property
    def waited_minutes(self) -> int:
        return int(self.waited.total_seconds() // 60)

    @property
    def stuck(self) -> bool:
        from core.health import SCAN_STUCK_MINUTES

        return self.waited > datetime.timedelta(minutes=SCAN_STUCK_MINUTES)


def _area(attachment: Attachment) -> str:
    if attachment.owner_type == "arc.arcrequest" and attachment.label in ARC_FORM_LABELS:
        return "ARC neighbor acknowledgement form"
    return AREAS.get(attachment.owner_type, "Other attachment")


def _words(result: str, detail: str) -> str:
    if result == ScanResult.UNSCANNED:
        return DETAIL_WORDS["not_configured"]
    if detail in DETAIL_WORDS:
        return DETAIL_WORDS[detail]
    return "Scanner error"  # recorded before reasons were kept


def _attempted(result: str) -> bool:
    """An answer was asked of a scanner (not "scanning was off")."""
    return result != ScanResult.UNSCANNED


def _waiting_since(uploaded_at, history) -> datetime.datetime:
    """When the current wait began: the first blocked scan after the last
    scan that gave an answer, else the upload. ``history`` is oldest first."""
    since = uploaded_at
    answered = False
    for result, _detail, at in history:
        if result in BLOCKED:
            if answered:
                since, answered = at, False
        else:
            answered = True
    return since


def _summarise(kind, obj, area, history) -> WaitingFile:
    attempts = [h for h in history if _attempted(h[0])]
    last = history[-1]
    return WaitingFile(
        kind=kind,
        pk=obj.pk,
        area=area,
        uploaded_at=obj.uploaded_at,
        waiting_since=_waiting_since(obj.uploaded_at, history),
        size=obj.size,
        attempts=len(attempts),
        last_result=_words(last[0], last[1]),
        last_attempt_at=attempts[-1][2] if attempts else None,
    )


def _waiting_versions():
    latest = VersionScan.objects.filter(version=OuterRef("pk")).values("result")[:1]
    return DocumentVersion.objects.annotate(latest=Subquery(latest)).filter(latest__in=BLOCKED)


def _waiting_attachments():
    latest = AttachmentScan.objects.filter(attachment=OuterRef("pk")).values("result")[:1]
    return Attachment.objects.annotate(latest=Subquery(latest)).filter(
        Q(latest__in=BLOCKED) | Q(latest__isnull=True, scan_result__in=BLOCKED)
    )


def waiting_count() -> int:
    with system_access("files waiting for a malware scan (count)"):
        return _waiting_versions().count() + _waiting_attachments().count()


def waiting_files() -> list[WaitingFile]:
    """Every file waiting for a scan, longest wait first (operations data only)."""
    out = []
    with system_access("files waiting for a malware scan"):
        for version in _waiting_versions().only("id", "uploaded_at", "size"):
            history = [
                (s.result, s.detail, s.scanned_at)
                for s in VersionScan.objects.filter(version=version).order_by("scanned_at", "id")
            ]
            out.append(_summarise(VERSION, version, "Document library", history))
        for attachment in _waiting_attachments().only(
            "id", "uploaded_at", "size", "owner_type", "label", "scan_result", "scan_detail"
        ):
            history = [(attachment.scan_result, attachment.scan_detail, attachment.uploaded_at)]
            history += [
                (s.result, s.detail, s.scanned_at)
                for s in AttachmentScan.objects.filter(attachment=attachment).order_by(
                    "scanned_at", "id"
                )
            ]
            out.append(_summarise(ATTACHMENT, attachment, _area(attachment), history))
    out.sort(key=lambda f: f.waiting_since)
    return out


def rescan(kind: str, pk: int) -> str | None:
    """Scan one stored file again and record the result as a new append-only
    scan row. Returns the result, or None when the file is not waiting any
    more (already scanned by another run) or does not exist."""
    if kind not in KINDS:
        raise ValueError("unknown file kind")
    with system_access("malware rescan of a waiting file"):
        queryset = _waiting_versions() if kind == VERSION else _waiting_attachments()
        obj = queryset.filter(pk=pk).first()
        if obj is None:
            return None
        with obj.file.open("rb") as fh:
            outcome = scan(fh)
        result, scanner = outcome
        row = {"result": result, "scanner": scanner, "detail": detail_of(outcome)}
        if kind == VERSION:
            VersionScan.objects.create(version=obj, **row)
        else:
            AttachmentScan.objects.create(attachment=obj, **row)
    log.info("documents.rescanned", extra={"kind": kind, "result": result})
    return result


def _defer(kind: str, pk: int, *, delay_seconds: int = 0) -> bool:
    """Queue one file's scan; False when that file already has one queued."""
    from procrastinate.exceptions import AlreadyEnqueued

    from documents.tasks import scan_file

    options = {"queueing_lock": f"scan-{kind}-{pk}"}
    if delay_seconds:
        options["schedule_in"] = {"seconds": delay_seconds}
    try:
        scan_file.configure(**options).defer(kind=kind, pk=pk)
    except AlreadyEnqueued:
        return False
    return True


def queue_soon(kind: str, pk: int) -> None:
    """After the upload commits, have the worker try this file again shortly."""
    if settings.DOCUMENT_SCANNER == "none":
        return
    transaction.on_commit(lambda: _defer(kind, pk, delay_seconds=RETRY_AFTER_UPLOAD_SECONDS))


def queue_waiting(*, actor, request=None) -> int:
    """System status "Scan waiting files now": queue the scan task for every
    waiting file. Sysadmins (system.run_task). Audited with counts only.
    Returns how many were queued (files already queued are not queued twice)."""
    from audit.services import record_event
    from core import policy

    policy.require(actor, "system.run_task")
    files = waiting_files()
    queued = sum(_defer(f.kind, f.pk) for f in files)
    record_event(
        "system.scan_waiting_queued",
        actor=actor,
        request=request,
        metadata={"waiting": len(files), "queued": queued},
    )
    return queued


def sweep(limit: int = SWEEP_LIMIT) -> int:
    """The worker's periodic retry: when the scanner answers, scan the files
    that have waited longest (at most ``limit``). Returns how many were
    scanned. Does nothing while scanning is off or the scanner is silent, so
    no scan rows pile up during an outage."""
    from core.health import scanner_version

    if settings.DOCUMENT_SCANNER != "clamd" or not scanner_version()["answering"]:
        return 0
    done = 0
    for item in waiting_files()[:limit]:
        if rescan(item.kind, item.pk) is not None:
            done += 1
    return done


def oldest_wait_minutes(files=None) -> int | None:
    files = waiting_files() if files is None else files
    return max((f.waited_minutes for f in files), default=None)

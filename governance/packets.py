"""One-click meeting packets (M5).

Two variants, built on a meeting page by the board or the manager:

* Board packet: cover, agenda, draft minutes of the previous meeting for
  approval, financial summary (totals only, with receivables aging marked
  "Confidential - board only"), open-items counts (also confidential) and an
  attachment index. Filed in the board-only "Meeting packets (board)" folder.
* Owner packet (not for executive sessions): cover, agenda, the previous
  meeting's approved minutes and the financial summary totals. Nothing about
  delinquency, violations or collections of any kind. Filed in the
  owner-visible "Meeting packets" folder, which publishes it.

The PDF comes from core/pdf.py and is saved through the documents service as
a new version of one document per meeting and variant, so it is immutable,
scanned and access-controlled like any document. Attachments are library
documents chosen by the builder; they are never opened or parsed. Each
attachment's exact version and scan status is recorded on the packet, and
the ".zip" download bundles the PDF with the attachments that pass the
download rules at that moment (others are listed as not included).
"""

from __future__ import annotations

import datetime
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from core import pdf
from core.money import format_cents
from core.policy import SYSTEM, can, require
from core.scoping import system_access
from documents.models import Category, Document, DocumentVersion, Folder, ScanResult, Visibility
from governance.models import (
    Meeting,
    MeetingKind,
    MeetingPacket,
    MeetingPacketItem,
    MeetingStatus,
    PacketVariant,
)

MAX_ATTACHMENTS = 20
# Bundled into one .zip at most; beyond this the rest stay in Documents.
ZIP_MAX_BYTES = 150 * 1024 * 1024
FOLDER_NAMES = {
    PacketVariant.OWNER: ("Meeting packets", Visibility.OWNERS),
    PacketVariant.BOARD: ("Meeting packets (board)", Visibility.BOARD),
}
CONFIDENTIAL = "Confidential \u2013 board only"


class PacketError(ValueError):
    pass


def _user(actor):
    return None if actor is SYSTEM else actor


# --- What can go in a packet --------------------------------------------------------------


def _visibilities(variant: str) -> list[str]:
    if variant == PacketVariant.OWNER:
        return [Visibility.OWNERS]
    return [Visibility.OWNERS, Visibility.BOARD]


def _packet_folders():
    return Folder.objects.filter(
        parent__isnull=True, name__in=[name for name, _ in FOLDER_NAMES.values()]
    )


def attachment_choices(user, variant: str):
    """Library documents the builder may attach: current, not archived,
    owner-wide (owner packet) or owner-wide and board-only (board packet).
    Never lot or committee documents, and never other packets."""
    return (
        Document.objects.for_user(user)
        .filter(
            archived=False,
            current_version__isnull=False,
            visibility__in=_visibilities(variant),
        )
        .exclude(folder__in=_packet_folders())
        .select_related("current_version")
        .order_by("-created_at", "-id")
    )


def minutes_choices(user):
    return attachment_choices(user, PacketVariant.BOARD).filter(category=Category.MINUTES)


def previous_meeting(meeting: Meeting) -> Meeting | None:
    """The last held meeting of the same kind before this one."""
    return (
        Meeting.objects.filter(
            kind=meeting.kind, status=MeetingStatus.HELD, starts_at__lt=meeting.starts_at
        )
        .exclude(pk=meeting.pk)
        .order_by("-starts_at")
        .first()
    )


def approved_minutes(meeting: Meeting) -> Document | None:
    """The previous meeting's minutes when the board has marked their current
    version approved (SEC-003: attaching is only a draft, and the manager may
    attach) and owners may see them."""
    from governance.services import minutes_approval

    previous = previous_meeting(meeting)
    if previous is None or minutes_approval(previous) is None:
        return None
    with system_access("packet: approved minutes"):
        doc = (
            Document.objects.filter(
                pk=previous.minutes_document_id,
                visibility=Visibility.OWNERS,
                archived=False,
                current_version__isnull=False,
            )
            .select_related("current_version")
            .first()
        )
    return doc


def default_draft_minutes(user, meeting: Meeting) -> Document | None:
    previous = previous_meeting(meeting)
    choices = minutes_choices(user)
    if previous and previous.minutes_document_id:
        found = choices.filter(pk=previous.minutes_document_id).first()
        if found:
            return found
    if previous:
        return choices.filter(created_at__gte=previous.starts_at).first()
    return None


# --- Figures ------------------------------------------------------------------------------


def _as_of(meeting: Meeting) -> datetime.date:
    return min(timezone.localdate(), timezone.localdate(meeting.starts_at))


def _fiscal_year(day: datetime.date) -> int:
    from community.models import CommunityProfile

    start = CommunityProfile.get().fiscal_year_start_month
    return day.year if day.month >= start else day.year - 1


def financial_summary(as_of: datetime.date, *, board: bool) -> dict:
    """Totals only. Receivables aging is included for the board packet only."""
    from community.models import CommunityProfile, Lot
    from ledger import accounting
    from ledger import services as ledger
    from ledger.models import Budget, BudgetStatus

    out: dict = {
        "as_of": as_of,
        "cash": accounting.book_balance(as_of),
        "reserves": CommunityProfile.get().resale_reserves_cents,
    }
    fy = _fiscal_year(as_of)
    budget = Budget.objects.filter(fiscal_year=fy).first()
    if budget is not None and not board and budget.status != BudgetStatus.ADOPTED:
        budget = None  # owners see adopted budgets only in a packet
    if budget is not None:
        months = ledger.fiscal_months(fy)
        elapsed = [i for i, (y, m) in enumerate(months) if (y, m) <= (as_of.year, as_of.month)]
        data = ledger.budget_vs_actual(budget)
        totals = {"income": [0, 0], "expense": [0, 0]}
        for row in data["rows"]:
            key = "income" if row["line"].kind == "income" else "expense"
            totals[key][0] += sum(row["planned"][i] for i in elapsed)
            totals[key][1] += sum(row["actual"][i] for i in elapsed)
        out["budget"] = {
            "label": f"{budget} ({budget.get_status_display().lower()})",
            "months": len(elapsed),
            "income": totals["income"],
            "expense": totals["expense"],
        }
    if board:
        buckets = dict.fromkeys(ledger.BUCKETS, 0)
        credit = 0
        with system_access("packet: receivables totals"):
            lot_ids = list(Lot.objects.values_list("pk", flat=True))
        for lot_id in lot_ids:
            a = ledger.aging(lot_id, as_of)
            for b in ledger.BUCKETS:
                buckets[b] += a[b]
            credit += a["credit"]
        out["aging"] = {
            "rows": [(ledger.BUCKET_LABELS[b], buckets[b]) for b in ledger.BUCKETS],
            "credit": credit,
            "total": sum(buckets.values()),
        }
    return out


def open_items() -> dict:
    """Counts for the board packet: no names, no lots, no balances."""
    from arc.models import ArcRequest, ArcState
    from compliance.models import Violation, ViolationState
    from ledger.models import CollectionCase
    from operations.models import OPEN_REQUEST_STATES, Invoice, OwnerRequest

    with system_access("packet: open-item counts"):
        arc = list(
            ArcRequest.objects.filter(
                state__in=[ArcState.SUBMITTED, ArcState.IN_REVIEW, ArcState.APPEAL]
            )
            .order_by("review_due_on", "pk")
            .values_list("pk", "state", "review_due_on", "appeal_due_on")
        )
        return {
            "arc": [
                (pk, appeal_due if state == ArcState.APPEAL else review_due)
                for pk, state, review_due, appeal_due in arc
            ],
            "violations": Violation.objects.filter(
                state__in=[
                    ViolationState.OPEN,
                    ViolationState.NOTICED,
                    ViolationState.RESPONDED,
                    ViolationState.HEARING,
                ]
            ).count(),
            "requests": OwnerRequest.objects.filter(state__in=OPEN_REQUEST_STATES).count(),
            "invoices": Invoice.objects.filter(state="pending_approval").count(),
            "collections": CollectionCase.objects.exclude(state="closed").count(),
        }


# --- The PDF ------------------------------------------------------------------------------


def _day(d) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _row(label: str, *values: str, width: int = 34) -> str:
    return label[:width].ljust(width) + "".join(v.rjust(14) for v in values)


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _skip_reason(version: DocumentVersion) -> str:
    from documents.services import downloadable

    if downloadable(version):
        return ""
    latest = version.scans.first()
    if latest and latest.result in (ScanResult.INFECTED, ScanResult.ERROR):
        return "did not pass the malware scan"
    return "waiting for a clean malware scan"


def render_packet(
    meeting: Meeting,
    variant: str,
    items: list[dict],
    *,
    minutes_note: str,
    finance: dict,
    counts: dict | None,
) -> bytes:
    from community.models import CommunityProfile

    board = variant == PacketVariant.BOARD
    name = "Board packet" if board else "Owner packet"
    when = timezone.localtime(meeting.starts_at)
    p: list[tuple[str, str]] = [
        ("small", pdf.plain(CommunityProfile.get().legal_name)),
        ("heading", name),
        ("normal", ""),
        ("heading", pdf.plain(meeting.title)),
        ("normal", pdf.plain(meeting.get_kind_display())),
        ("normal", f"{_day(when)} at {when:%-I:%M %p}"),
    ]
    if meeting.location:
        p.append(("normal", pdf.plain(f"Location: {meeting.location}")))
    p += [("normal", ""), ("small", f"Prepared {_day(timezone.localdate())}.")]
    if board:
        p += [
            ("normal", ""),
            ("heading", CONFIDENTIAL.upper()),
            ("normal", "For the board. Do not share with owners or post to the owner library."),
        ]
    contents = ["Agenda", "Minutes", "Financial summary"]
    if board:
        contents.append(f"Open items ({CONFIDENTIAL.lower()})")
    contents.append("Attachment index")
    p += [("normal", ""), ("normal", "**Contents**")]
    p += [("normal", f"{i}. {c}") for i, c in enumerate(contents, start=1)]

    p += [("pagebreak", ""), ("heading", "1. Agenda")]
    p.append(("normal", pdf.plain(meeting.agenda) if meeting.agenda.strip() else "No agenda yet."))

    p += [("normal", ""), ("heading", "2. Minutes"), ("normal", minutes_note)]

    p += [("pagebreak", ""), ("heading", "3. Financial summary")]
    p.append(("small", f"As of {_day(finance['as_of'])}. Totals only."))
    p.append(("mono", _row("Operating cash (cash book)", format_cents(finance["cash"]))))
    reserves = finance["reserves"]
    p.append(
        (
            "mono",
            _row(
                "Reserve funds (board's figure)",
                format_cents(reserves) if reserves is not None else "not recorded",
            ),
        )
    )
    p.append(("normal", ""))
    budget = finance.get("budget")
    if budget:
        p.append(("normal", f"**Budget vs. actual, year to date** ({pdf.plain(budget['label'])})"))
        p.append(("small", f"{budget['months']} of 12 months of the fiscal year."))
        p.append(("mono", _row("", "Budget", "Actual", "Variance")))
        for label, (planned, actual) in (
            ("Income", budget["income"]),
            ("Expenses", budget["expense"]),
        ):
            p.append(
                (
                    "mono",
                    _row(
                        label,
                        format_cents(planned),
                        format_cents(actual),
                        format_cents(actual - planned),
                    ),
                )
            )
        net_plan = budget["income"][0] - budget["expense"][0]
        net_actual = budget["income"][1] - budget["expense"][1]
        p.append(
            (
                "mono",
                _row(
                    "Net",
                    format_cents(net_plan),
                    format_cents(net_actual),
                    format_cents(net_actual - net_plan),
                ),
            )
        )
    else:
        p.append(("normal", "No budget for this fiscal year is available."))
    if board and "aging" in finance:
        aging = finance["aging"]
        p += [("normal", ""), ("normal", f"**Receivables aging \u2013 {CONFIDENTIAL.lower()}**")]
        p.append(("small", "Totals across all accounts; no names or lot balances."))
        for label, cents in aging["rows"]:
            p.append(("mono", _row(label, format_cents(cents))))
        p.append(("mono", _row("Total owed", format_cents(aging["total"]))))
        p.append(("mono", _row("Credits on account", format_cents(aging["credit"]))))

    section = 4
    if board and counts is not None:
        p += [("pagebreak", ""), ("heading", f"4. Open items \u2013 {CONFIDENTIAL.lower()}")]
        p.append(("small", "Counts only. Details are on each page of the portal."))
        p.append(("mono", _row("ARC requests awaiting a decision", str(len(counts["arc"])))))
        for pk, due in counts["arc"][:15]:
            p.append(("mono", f"    Request {pk}: due {_day(due) if due else 'no date set'}"))
        if len(counts["arc"]) > 15:
            p.append(("mono", f"    and {len(counts['arc']) - 15} more"))
        p.append(("mono", _row("Violations open", str(counts["violations"]))))
        p.append(("mono", _row("Owner requests open", str(counts["requests"]))))
        p.append(("mono", _row("Invoices awaiting approval", str(counts["invoices"]))))
        p.append(("mono", _row("Collection cases open", str(counts["collections"]))))
        section = 5

    p += [("pagebreak", ""), ("heading", f"{section}. Attachment index")]
    if items:
        p.append(
            (
                "small",
                "Attachments are separate files, not reproduced here. Download "
                '"Packet with attachments (.zip)" from the meeting page.',
            )
        )
        for item in items:
            v = item["version"]
            p.append(
                (
                    "normal",
                    f"{item['position']}. {pdf.plain(item['document'].title)}"
                    f" (version {v.number}, {v.original_filename.rsplit('.', 1)[-1].upper()},"
                    f" {_size(v.size)})",
                )
            )
            if item["included"]:
                p.append(("small", "    Included in the .zip."))
            else:
                p.append(("small", f"    Not included: {item['skipped_reason']}."))
    else:
        p.append(("normal", "No attachments."))
    return pdf.render(p, title=f"{name}: {meeting.title}")


# --- Building and saving ------------------------------------------------------------------


def _folder(variant: str) -> Folder:
    from documents import services as documents

    name, visibility = FOLDER_NAMES[variant]
    existing = Folder.objects.filter(parent__isnull=True, name=name).first()
    if existing is not None:
        if existing.visibility != visibility:
            raise PacketError(
                f'The "{name}" folder has the wrong visibility; ask a board member to fix it.'
            )
        return existing
    return documents.create_folder(name=name, visibility=visibility, actor=SYSTEM)


def _minutes_note(meeting, variant, minutes_doc) -> str:
    previous = previous_meeting(meeting)
    if minutes_doc is None:
        if variant == PacketVariant.BOARD:
            return "No draft minutes were chosen for approval."
        return "No approved minutes from a previous meeting of this kind are on file."
    of = (
        f" of {pdf.plain(previous.title)} on {_day(timezone.localdate(previous.starts_at))}"
        if previous
        else ""
    )
    title = pdf.plain(minutes_doc.title)
    if variant == PacketVariant.BOARD:
        return (
            f"Draft minutes{of}: {title}, attachment 1 in the index. "
            "The board is asked to approve them."
        )
    from governance.services import minutes_approval

    approval = minutes_approval(previous) if previous else None
    on = f" (approved by the board on {_day(approval.approved_on)})" if approval else ""
    return f"Approved minutes{of}: {title}{on}, attachment 1 in the index."


def build_packet(
    meeting: Meeting,
    *,
    variant: str,
    attachment_ids=(),
    minutes_id=None,
    actor,
    request=None,
) -> MeetingPacket:
    """Build the PDF, save it as a new document version and record the build.
    ``meeting`` must have been fetched through ``Meeting.objects.for_user``."""
    from documents import services as documents

    require(actor, "meeting.packet_build")
    if variant not in PacketVariant.values:
        raise PacketError("Choose a board packet or an owner packet.")
    if meeting.status == MeetingStatus.CANCELLED:
        raise PacketError("This meeting was cancelled.")
    if variant == PacketVariant.OWNER and meeting.kind == MeetingKind.EXECUTIVE:
        raise PacketError("Executive sessions are board only: build a board packet.")
    ids = {int(i) for i in attachment_ids}
    if len(ids) > MAX_ATTACHMENTS:
        raise PacketError(f"Attach at most {MAX_ATTACHMENTS} documents.")
    chosen = list(attachment_choices(actor, variant).filter(pk__in=ids))
    if len(chosen) != len(ids):
        raise PacketError("One of the chosen documents can't go in this packet.")
    if variant == PacketVariant.BOARD:
        minutes_doc = None
        if minutes_id:
            minutes_doc = minutes_choices(actor).filter(pk=minutes_id).first()
            if minutes_doc is None:
                raise PacketError("Choose minutes from the Minutes documents.")
    else:
        minutes_doc = approved_minutes(meeting)
    docs = ([minutes_doc] if minutes_doc else []) + [d for d in chosen if d != minutes_doc]
    items = []
    for position, doc in enumerate(docs, start=1):
        version = doc.current_version
        reason = _skip_reason(version)
        items.append(
            {
                "position": position,
                "document": doc,
                "version": version,
                "role": "minutes" if doc == minutes_doc else "attachment",
                "included": not reason,
                "skipped_reason": reason,
            }
        )
    board = variant == PacketVariant.BOARD
    data = render_packet(
        meeting,
        variant,
        items,
        minutes_note=_minutes_note(meeting, variant, minutes_doc),
        finance=financial_summary(_as_of(meeting), board=board),
        counts=open_items() if board else None,
    )
    when = timezone.localdate(meeting.starts_at)
    upload = SimpleUploadedFile(
        f"{variant}-packet-{when:%Y-%m-%d}.pdf", data, content_type="application/pdf"
    )
    label = "Board packet" if board else "Owner packet"
    with transaction.atomic():
        Meeting.objects.select_for_update().filter(pk=meeting.pk).exists()
        earlier = MeetingPacket.objects.filter(meeting=meeting, variant=variant).first()
        try:
            if earlier is not None:
                version = documents.add_version(
                    earlier.document, file=upload, note="Rebuilt", actor=actor, request=request
                )
                document = earlier.document
            else:
                document = documents.create_document(
                    title=f"{label}: {meeting.title} ({when:%b} {when.day}, {when.year})"[:200],
                    category=Category.OTHER,
                    visibility=FOLDER_NAMES[variant][1],
                    folder=_folder(variant),
                    file=upload,
                    actor=actor,
                    request=request,
                )
                version = document.current_version
        except documents.DocumentError as exc:
            raise PacketError(str(exc)) from exc
        packet = MeetingPacket.objects.create(
            meeting=meeting,
            variant=variant,
            document=document,
            version=version,
            built_by=_user(actor),
        )
        MeetingPacketItem.objects.bulk_create(
            MeetingPacketItem(
                packet=packet,
                position=i["position"],
                document=i["document"],
                version=i["version"],
                role=i["role"],
                included=i["included"],
                skipped_reason=i["skipped_reason"],
            )
            for i in items
        )
        record_event(
            "meeting.packet_built",
            actor=_user(actor),
            request=request,
            target=meeting,
            metadata={
                "variant": variant,
                "document_id": document.pk,
                "version": version.number,
                "attachments": len(items),
                "skipped": sum(1 for i in items if not i["included"]),
            },
        )
    return packet


# --- Reading ------------------------------------------------------------------------------


def visible_packets(user, meeting: Meeting) -> dict[str, MeetingPacket]:
    """The latest packet of each variant whose document this person may open."""
    out = {}
    for variant in PacketVariant.values:
        packet = (
            MeetingPacket.objects.filter(meeting=meeting, variant=variant)
            .select_related("document", "version")
            .first()
        )
        if packet and Document.objects.for_user(user).filter(pk=packet.document_id).exists():
            packet.not_in_zip = _not_in_zip(packet)
            out[variant] = packet
    return out


def _not_in_zip(packet: MeetingPacket) -> int:
    """How many attachments the .zip leaves out for a reason known without
    opening the file store (skipped at build, or not passing the scan rule);
    the meeting page says so next to the button (WT-003). Files missing from
    the store are found while downloading and listed in NOT-INCLUDED.txt."""
    from documents.services import downloadable

    return sum(
        1
        for item in packet.items.select_related("version")
        if item.skipped_reason or not downloadable(item.version)
    )


def packet_for(user, meeting: Meeting, packet_id) -> MeetingPacket | None:
    """A packet of this (already scoped) meeting that this person may open.
    People without board access get the latest build only (owners see a
    document's current version, never earlier ones)."""
    packet = (
        MeetingPacket.objects.filter(meeting=meeting, pk=packet_id)
        .select_related("document", "version")
        .first()
    )
    if packet is None:
        return None
    if not Document.objects.for_user(user).filter(pk=packet.document_id).exists():
        return None
    if not can(user, "document.view_board"):
        latest = MeetingPacket.objects.filter(meeting=meeting, variant=packet.variant).first()
        if latest.pk != packet.pk or packet.document.current_version_id != packet.version_id:
            return None
    return packet


def _zip_name(position: int, version: DocumentVersion) -> str:
    from documents.validation import download_filename

    return f"{position:02d}-{download_filename(version.original_filename, version.content_type)}"


# --- The .zip download (streamed) ----------------------------------------------------------
# SEC-002 / R2-003: the archive is never held in memory or on the /tmp tmpfs
# (which counts against the container's memory). It is written straight into
# the response a chunk at a time (zipfile on an unseekable sink uses data
# descriptors), so a download holds about one chunk plus the compressor's state.
ZIP_CHUNK_BYTES = 256 * 1024
# One .zip at a time per person, and at most this many per window (counted from
# the audit log, so it holds across web processes).
ZIP_RATE_LIMIT = 10
ZIP_RATE_WINDOW = datetime.timedelta(minutes=15)
# A download may run this long; then the stream stops (fixres SEC-002).
ZIP_MAX_SECONDS = 30 * 60
# The person's slot lives as long as the stream: it is renewed whenever a
# chunk is sent and freed when the response closes; one left behind by a
# dead process expires this long after its last chunk.
ZIP_SLOT_SECONDS = ZIP_MAX_SECONDS + 10 * 60
ZIP_RENEW_SECONDS = 60  # renew at most this often (one cache write)


class PacketBusy(PacketError):
    """Too many .zip downloads by this person right now (rate limit)."""


def _slot_key(user) -> str:
    return f"packet-zip-slot:{user.pk}"


def _take_slot(user) -> bool:
    from django.core.cache import cache

    return cache.add(_slot_key(user), 1, timeout=ZIP_SLOT_SECONDS)


def _renew_slot(user) -> None:
    from django.core.cache import cache

    cache.touch(_slot_key(user), timeout=ZIP_SLOT_SECONDS)


def _free_slot(user) -> None:
    from django.core.cache import cache

    cache.delete(_slot_key(user))


def _recent_downloads(user) -> int:
    from audit.models import AuditEvent

    since = timezone.now() - ZIP_RATE_WINDOW
    return AuditEvent.objects.filter(
        action="meeting.packet_zip_downloaded", actor=user, occurred_at__gte=since
    ).count()


class _Sink:
    """Write-only target for ZipFile: keeps only what was written since the
    last drain (no tell/seek, so zipfile streams with data descriptors)."""

    def __init__(self):
        self._parts: list[bytes] = []
        self.held = 0
        self.most_held = 0

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        self.held += len(data)
        self.most_held = max(self.most_held, self.held)
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        self.held = 0
        return data


_NOTICE_HEAD = "These files are listed in the index but not in this .zip:\r\n\r\n"
UNREADABLE = "the stored file could not be read; ask the board for a copy"


def _readable(version: DocumentVersion) -> bool:
    """The stored file is there (WT-003): a version whose file is missing from
    the file store is left out of the .zip and listed, never a server error."""
    try:
        return bool(version.file.name) and version.file.storage.exists(version.file.name)
    except (OSError, ValueError):
        return False


class PacketZipStream:
    """Iterator of the .zip's bytes for a StreamingHttpResponse. ``close()``
    (called by Django when the response ends, even if never iterated) frees the
    person's download slot."""

    def __init__(self, entries, notice: str, stamp, user):
        self._entries = entries
        self._notice = notice
        # Files that could not be opened while streaming (WT-003): listed in
        # NOT-INCLUDED.txt at the end of the archive, never a server error.
        self.unreadable: list[str] = []
        self._stamp = stamp
        self._user = user
        self.sink = _Sink()
        self._gen = self._chunks()
        self._closed = False
        self._started = timezone.now()
        self._renewed = self._started

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        now = timezone.now()
        if (now - self._started).total_seconds() > ZIP_MAX_SECONDS:
            # Too slow: stop, so one download never holds a web thread for long.
            self.close()
            raise StopIteration
        chunk = next(self._gen)
        if (now - self._renewed).total_seconds() >= ZIP_RENEW_SECONDS:
            _renew_slot(self._user)  # still sending: the slot stays taken
            self._renewed = now
        return chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._gen.close()
        finally:
            _free_slot(self._user)

    def _chunks(self):
        sink = self.sink
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, version in self._entries:
                info = zipfile.ZipInfo(name, date_time=self._stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                try:
                    src = version.file.open("rb")
                except (OSError, ValueError):
                    # Gone from the file store since the check in packet_zip:
                    # opened before the entry starts, so the archive stays whole.
                    self.unreadable.append(name)
                    continue
                with src, archive.open(info, "w") as dst:
                    while chunk := src.read(ZIP_CHUNK_BYTES):
                        dst.write(chunk)
                        if sink.held:
                            yield sink.drain()
                if sink.held:
                    yield sink.drain()
            notice = self._notice
            if self.unreadable:
                if not notice:
                    notice = _NOTICE_HEAD
                notice += "".join(
                    f"{name}: Not included: the stored file could not be read.\r\n"
                    for name in self.unreadable
                )
            if notice:
                info = zipfile.ZipInfo("NOT-INCLUDED.txt", date_time=self._stamp)
                archive.writestr(info, notice)
        if sink.held:
            yield sink.drain()


def packet_zip(packet: MeetingPacket, *, user, request=None) -> tuple[PacketZipStream, str]:
    """The packet PDF plus every attachment that may be downloaded now by this
    person, as a stream. Entry names are generated (number + sanitized name
    ending in the validated extension); file contents are copied, never
    opened. Raises PacketBusy when this person already has a download running
    or has reached ZIP_RATE_LIMIT in the window."""
    from documents.services import downloadable
    from documents.validation import safe_filename

    if not downloadable(packet.version):
        raise PacketError("The packet can't be downloaded until it passes a malware scan.")
    if _recent_downloads(user) >= ZIP_RATE_LIMIT:
        raise PacketBusy(
            "You have downloaded several packets in the last few minutes. Please try again later."
        )
    if not _take_slot(user):
        raise PacketBusy("Your other packet download is still running. Try again when it ends.")
    try:
        can_board = can(user, "document.view_board")
        visible = set(Document.objects.for_user(user).values_list("pk", flat=True))
        when = timezone.localdate(packet.meeting.starts_at)
        stamp = timezone.localtime(packet.built_at).timetuple()[:6]
        included, skipped = [], []
        entries = []
        if _readable(packet.version):
            entries.append((_zip_name(0, packet.version), packet.version))
        else:
            skipped.append(("The packet PDF", UNREADABLE + " or download the PDF again later"))
        total = packet.version.size
        for item in packet.items.select_related("document", "version"):
            reason = item.skipped_reason
            if not reason:
                if item.document_id not in visible or (
                    not can_board and item.document.current_version_id != item.version_id
                ):
                    reason = "no longer available to you"
                elif not downloadable(item.version):
                    reason = _skip_reason(item.version) or "not available"
                elif total + item.version.size > ZIP_MAX_BYTES:
                    reason = "too large to bundle; download it from Documents"
                elif not _readable(item.version):
                    reason = UNREADABLE
            if reason:
                skipped.append((item.position, reason))
                continue
            entries.append((_zip_name(item.position, item.version), item.version))
            total += item.version.size
            included.append(item)
        notice = ""
        if skipped:
            lines = [f"{pos}. Not included: {why}." for pos, why in skipped]
            notice = _NOTICE_HEAD + "\r\n".join(lines) + "\r\n"
        for item in included:
            record_event(
                "document.downloaded",
                request=request,
                actor=user,
                target=item.document,
                metadata={"version": item.version.number},
            )
        record_event(
            "meeting.packet_zip_downloaded",
            request=request,
            actor=user,
            target=packet.meeting,
            metadata={
                "variant": packet.variant,
                "packet_id": packet.pk,
                "files": len(entries),
                "skipped": len(skipped),
            },
        )
    except BaseException:
        _free_slot(user)
        raise
    filename = safe_filename(f"{packet.variant} packet {when:%Y-%m-%d}", "zip")
    return PacketZipStream(entries, notice, stamp, user), filename


__all__ = [
    "PacketBusy",
    "PacketError",
    "PacketZipStream",
    "attachment_choices",
    "build_packet",
    "minutes_choices",
    "packet_for",
    "packet_zip",
    "visible_packets",
]

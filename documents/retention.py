"""Records retention by document category (D72).

Each library category has a rule ``records.keep_years.<category>`` (years;
0 keeps forever) counted from a fixed point (``community.rules
.RECORDS_SCHEDULE``):

* ``date``: the document's date (``Document.dated_on``, else the day it was
  added); every version of the document is then due;
* ``superseded``: the day a newer version replaced it; only replaced
  versions are ever due, the current one never;
* ``contract_end``: the end of the contract whose renewal terms hold the
  document (documents/renewals.py), once that contract has ended and was not
  renewed by itself; a contract document no contract points at counts from
  its date.

Nothing is removed silently. The nightly task only lists what is due
(append-only ``RetentionScan``, counts only) and tells the board when the
list grows; the board's Records retention page shows it. A board member
proposes disposal of some or all of it with a reason (``DisposalRequest``);
a *different* board member confirms (``DisposalDecision``), and only then is
each stored file removed, with a permanent ``DocumentDisposal`` record (what,
the rule, when, who proposed, who confirmed, why). The version's row (name,
size, checksum, uploader) is never changed or deleted. A legal hold on the
document or its category stops disposal at every step. A document whose
every version has been disposed of is archived (owners no longer see it).

Backups taken before a disposal still hold the file until they age out
under the backup schedule; the page says so.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.services import record_event
from community.rules import (
    AFTER_CONTRACT_END,
    AFTER_DATE,
    BASIS_PHRASES,
    RECORDS_SCHEDULE,
    rule,
)
from core.policy import require
from core.scoping import system_access
from documents.models import (
    Category,
    ContractTerms,
    DisposalDecision,
    DisposalRequest,
    DisposalRequestItem,
    Document,
    DocumentDisposal,
    DocumentVersion,
    LegalHold,
    LegalHoldRelease,
    RenewalType,
    RetentionScan,
)

log = logging.getLogger(__name__)

PERMISSION = "records_retention.manage"
BASIS = {category: basis for category, _label, basis, _d in RECORDS_SCHEDULE}
LABELS = {category: label for category, label, _b, _d in RECORDS_SCHEDULE}


class RetentionError(ValueError):
    pass


def rule_key(category: str) -> str:
    return f"records.keep_years.{category}"


def keep_years(category: str) -> int:
    if category not in BASIS:
        return 0
    return int(rule(rule_key(category)) or 0)


def plus_years(day: datetime.date, years: int) -> datetime.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def schedule() -> list[dict]:
    """The schedule as the board's page shows it."""
    return [
        {
            "category": category,
            "label": Category(category).label,
            "what": label,
            "basis": BASIS_PHRASES[basis],
            "years": keep_years(category),
            "key": rule_key(category),
        }
        for category, label, basis, _d in RECORDS_SCHEDULE
    ]


@dataclass
class DueVersion:
    version: DocumentVersion
    document: Document
    basis: str
    years: int
    kept_until: datetime.date
    held: bool = False
    pending: DisposalRequest | None = None

    @property
    def basis_phrase(self) -> str:
        return BASIS_PHRASES[self.basis]


def active_holds():
    return LegalHold.objects.filter(release__isnull=True)


def _held(document: Document, holds: list[LegalHold]) -> bool:
    return any(h.document_id == document.pk or h.category == document.category for h in holds)


def _contract_ends(today) -> dict[int, datetime.date]:
    """document id -> the day its contract ended (contracts that are over and
    did not renew by themselves). Documents of contracts still running are
    absent (and so never due)."""
    from documents.renewals import contract_term

    out: dict[int, datetime.date | None] = {}
    for terms in ContractTerms.objects.filter(document__isnull=False).select_related("contract"):
        contract = terms.contract
        end = None
        if contract.ends_on is not None and contract.state == "active":
            from documents.models import RenewalDecision

            decisions = {
                (d.contract_id, d.term_ends_on): d
                for d in RenewalDecision.objects.filter(contract=contract)
            }
            term_end, _deadline, decision = contract_term(contract, terms, today, decisions)
            auto = terms.renewal == RenewalType.AUTO and decision is None
            if term_end < today and not auto:
                end = term_end
        elif contract.state == "discarded":
            end = timezone.localdate(contract.created_at)
        previous = out.get(terms.document_id, datetime.date.min)
        # One document for several contracts: the latest end, and none while any runs.
        out[terms.document_id] = None if end is None or previous is None else max(previous, end)
    return {k: v for k, v in out.items() if v is not None}


def due(today: datetime.date | None = None) -> list[DueVersion]:
    """Versions past their retention period and not yet disposed of
    (held ones included, marked ``held``)."""
    today = today or timezone.localdate()
    out: list[DueVersion] = []
    with system_access("records retention: find due versions"):
        holds = list(active_holds())
        pending = {
            item.version_id: item.request
            for item in DisposalRequestItem.objects.filter(
                request__decision__isnull=True
            ).select_related("request")
        }
        disposed = set(DocumentDisposal.objects.values_list("version_id", flat=True))
        contract_ends, linked = None, set()
        for category, _label, basis, _d in RECORDS_SCHEDULE:
            years = keep_years(category)
            if years <= 0:
                continue
            documents = Document.objects.unscoped("retention").filter(category=category)
            # Every version ever stored (disposed ones too) decides which are
            # superseded and when; only those not disposed of can be due.
            versions = (
                DocumentVersion.objects.filter(document__in=documents)
                .select_related("document")
                .order_by("document_id", "number")
            )
            by_doc: dict[int, list[DocumentVersion]] = {}
            for v in versions:
                by_doc.setdefault(v.document_id, []).append(v)
            if basis == AFTER_CONTRACT_END and contract_ends is None:
                contract_ends = _contract_ends(today)
                linked = set(
                    ContractTerms.objects.filter(document__isnull=False).values_list(
                        "document_id", flat=True
                    )
                )
            for doc_versions in by_doc.values():
                document = doc_versions[0].document
                starts = _starts(document, doc_versions, basis, contract_ends, linked)
                for v, start in starts:
                    if start is None or v.pk in disposed:
                        continue
                    until = plus_years(start, years)
                    if until <= today:
                        out.append(
                            DueVersion(
                                v,
                                document,
                                basis,
                                years,
                                until,
                                held=_held(document, holds),
                                pending=pending.get(v.pk),
                            )
                        )
    return sorted(out, key=lambda d: (d.kept_until, d.document.title, d.version.number))


def _starts(document, versions, basis, contract_ends, linked):
    """(version, the day its retention period starts or None) pairs, for
    every stored version of one document."""
    numbers = {v.number: v.uploaded_at for v in versions}
    doc_date = document.dated_on or timezone.localdate(document.created_at)
    if basis == AFTER_CONTRACT_END:
        if document.pk in (contract_ends or {}):
            return [(v, contract_ends[document.pk]) for v in versions]
        # Not linked to a contract's terms at all: from its date. Linked but
        # the contract still runs: not yet.
        return [(v, None if document.pk in linked else doc_date) for v in versions]
    if basis == AFTER_DATE:
        return [(v, doc_date) for v in versions]
    out = []
    for v in versions:
        if v.pk == document.current_version_id or v.number + 1 not in numbers:
            out.append((v, None))
        else:
            out.append((v, timezone.localdate(numbers[v.number + 1])))
    return out


def preview(today=None) -> dict:
    items = due(today)
    return {
        "ready": [d for d in items if not d.held and d.pending is None],
        "pending": [d for d in items if not d.held and d.pending is not None],
        "held": [d for d in items if d.held],
    }


def pending_requests():
    return (
        DisposalRequest.objects.filter(decision__isnull=True)
        .select_related("proposed_by")
        .prefetch_related("items__version__document")
    )


# --- Legal holds -------------------------------------------------------------------------


def place_hold(*, document=None, category: str = "", reason: str, actor, request=None):
    require(actor, PERMISSION)
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        raise RetentionError("Say why the hold is needed (a claim, a dispute, an audit).")
    if (document is None) == (not category):
        raise RetentionError("Choose one document or one category.")
    if category and category not in Category.values:
        raise RetentionError("Choose a category.")
    with transaction.atomic():
        hold = LegalHold.objects.create(
            document=document, category=category or "", reason=reason, placed_by=actor
        )
        record_event(
            "records.hold_placed",
            actor=actor,
            request=request,
            target=hold,
            metadata={
                "hold_id": hold.pk,
                "document_id": document.pk if document else None,
                "category": category or None,
            },
        )
    return hold


def release_hold(hold: LegalHold, *, reason: str, actor, request=None):
    require(actor, PERMISSION)
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        raise RetentionError("Say why the hold can end.")
    from django.db import IntegrityError

    try:
        with transaction.atomic():
            release = LegalHoldRelease.objects.create(hold=hold, reason=reason, released_by=actor)
            record_event(
                "records.hold_released",
                actor=actor,
                request=request,
                target=hold,
                metadata={"hold_id": hold.pk},
            )
    except IntegrityError as exc:
        raise RetentionError("This hold has already been released.") from exc
    return release


# --- Two-person disposal -------------------------------------------------------------------


def propose(version_ids, *, reason: str, actor, request=None, today=None) -> DisposalRequest:
    require(actor, PERMISSION)
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        raise RetentionError("Give a reason (for example the retention schedule's resolution).")
    wanted = {int(v) for v in version_ids}
    if not wanted:
        raise RetentionError("Choose at least one file.")
    ready = {d.version.pk: d for d in preview(today)["ready"]}
    missing = wanted - set(ready)
    if missing:
        raise RetentionError(
            "Some chosen files are not due, are under a legal hold, or already wait for a "
            "second board member. Reload the page and choose again."
        )
    with transaction.atomic():
        req = DisposalRequest.objects.create(reason=reason, proposed_by=actor)
        for pk in sorted(wanted):
            DisposalRequestItem.objects.create(request=req, version=ready[pk].version)
        record_event(
            "records.disposal_proposed",
            actor=actor,
            request=request,
            target=req,
            metadata={"request_id": req.pk, "files": len(wanted)},
        )
    return req


def decide(req: DisposalRequest, *, approve: bool, note: str = "", actor, request=None, today=None):
    """Confirm (a board member other than the proposer) or decline (anyone
    on the board, the proposer included: a withdrawal). Returns the number
    of files disposed of."""
    require(actor, PERMISSION)
    note = " ".join((note or "").split())[:300]
    if approve and req.proposed_by_id == getattr(actor, "pk", None):
        raise RetentionError("A different board member must confirm a disposal you proposed.")
    from django.db import IntegrityError

    disposed = skipped = 0
    try:
        with transaction.atomic():
            req = DisposalRequest.objects.select_for_update().get(pk=req.pk)
            DisposalDecision.objects.create(
                request=req, approved=approve, note=note, decided_by=actor
            )
            if approve:
                # Re-check now: a hold placed or a rule changed since the
                # proposal stops that file.
                items = list(req.items.select_related("version__document"))
                still_due = {d.version.pk: d for d in due(today) if not d.held}
                for item in items:
                    d = still_due.get(item.version_id)
                    if d is None:
                        skipped += 1
                        continue
                    _dispose(d, req, actor)
                    disposed += 1
                record_event(
                    "records.disposal_confirmed",
                    actor=actor,
                    request=request,
                    target=req,
                    metadata={"request_id": req.pk, "disposed": disposed, "skipped": skipped},
                )
            else:
                record_event(
                    "records.disposal_declined",
                    actor=actor,
                    request=request,
                    target=req,
                    metadata={"request_id": req.pk},
                )
    except IntegrityError as exc:
        raise RetentionError("This disposal has already been decided.") from exc
    return disposed


def _dispose(d: DueVersion, req: DisposalRequest, actor) -> None:
    version = d.version
    DocumentDisposal.objects.create(
        version=version,
        document=d.document,
        request=req,
        category=d.document.category,
        basis=d.basis,
        years=d.years,
        kept_until=d.kept_until,
        reason=req.reason,
        proposed_by=req.proposed_by,
        confirmed_by=actor,
    )
    # The file goes once the record is committed, so a file is never removed
    # without its record; if the file store refuses then, the nightly scan
    # removes it.
    name, storage = version.file.name, version.file.storage
    if name:
        transaction.on_commit(lambda: _remove_file(storage, name))
    with system_access("archive fully disposed document"):
        remaining = (
            DocumentVersion.objects.filter(document=d.document)
            .exclude(pk__in=DocumentDisposal.objects.values("version_id"))
            .exists()
        )
        if not remaining and not d.document.archived:
            Document.objects.unscoped("archive disposed").filter(pk=d.document.pk).update(
                archived=True
            )


def _remove_file(storage, name: str) -> bool:
    try:
        if storage.exists(name):
            storage.delete(name)
            return True
    except OSError:
        log.warning("records_retention.file_delete_failed")
    return False


def scan(today=None) -> RetentionScan:
    """The nightly check (SYSTEM): count what is due and held, remove any
    file a recorded disposal left behind, tell the board when more is due
    than at the last check."""
    from django.conf import settings
    from django.urls import reverse

    items = due(today)
    ready = sum(1 for d in items if not d.held)
    held = sum(1 for d in items if d.held)
    removed = 0
    with system_access("records retention: leftover files"):
        recent = timezone.now() - datetime.timedelta(days=30)
        for disposal in DocumentDisposal.objects.filter(disposed_at__gte=recent).select_related(
            "version"
        ):
            name = disposal.version.file.name
            if name and _remove_file(disposal.version.file.storage, name):
                removed += 1
        last = RetentionScan.objects.first()
    with transaction.atomic():
        row = RetentionScan.objects.create(due=ready, held=held, files_removed=removed)
        record_event(
            "records.retention_scan",
            actor=None,
            target=row,
            metadata={"due": ready, "held": held, "removed": removed},
        )
    if ready and (last is None or ready > last.due):
        from comms.notify import mail_board
        from community.services import board_emails

        body = (
            f"{ready} file{'s are' if ready != 1 else ' is'} past the retention period in the "
            f"document library. Nothing has been removed: a board member can propose disposal "
            f"and a second board member confirms it.\n\n"
            f"{settings.PORTAL_BASE_URL}{reverse('records_retention')}\n\n-- \n{settings.SITE_NAME}"
        )
        mail_board(board_emails(), subject="Records past their retention period", body=body)
    return row


def my_items(user, today=None) -> list:
    from django.urls import reverse

    from core import policy
    from core.my_items import Item

    if not policy.can(user, PERMISSION):
        return []
    out = []
    p = preview(today)
    if p["ready"]:
        n = len(p["ready"])
        out.append(
            Item(
                "Records retention",
                f"{n} file{'s' if n != 1 else ''} past the retention period",
                "Review them; disposal needs two board members",
                reverse("records_retention"),
                "info",
                nav="records_retention",
                icon="clock",
            )
        )
    for req in pending_requests():
        if req.proposed_by_id == user.pk:
            continue
        n = len(req.items.all())
        out.append(
            Item(
                "Records retention",
                f"Confirm disposal of {n} file{'s' if n != 1 else ''}",
                f"Proposed {req.proposed_at:%b %-d}: {req.reason[:80]}",
                reverse("records_retention") + f"#request-{req.pk}",
                "warning",
                timezone.localdate(req.proposed_at),
                nav="records_retention",
                icon="clock",
            )
        )
    return out


def holds_for(document) -> list[LegalHold]:
    return list(active_holds().filter(Q(document=document) | Q(category=document.category)))

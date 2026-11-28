"""USPS tracking of paper mail: Informed Visibility scans (D60).

When the board switches on "usps_tracking" (Settings → Features (board)) and a
sysadmin has put the HOA's Business Customer Gateway login on the server
(infra/poc/set-usps.sh), a background task asks USPS a few times a day for
the scans of each barcoded letter (``MailPiece``) that was marked as mailed
in the last 30 days and has not reached a final state. Each new scan is
stored once, append-only (``MailPieceScan``). Pull only: USPS never calls us.

Statuses, in plain words (``status_for``):

* Mailed: marked as mailed, no scan yet.
* Processed: USPS has scanned it on its way.
* Sorted for delivery on <date>: sorted into the carrier's delivery order
  (operation codes 918/919). This usually means delivered that day or the
  next: USPS gives actual delivery scans only to mailers who file an
  electronic mailing statement (eDoc), which the association does not.
* Delivered on <date>: a delivery scan (517), should one ever appear.
* No scans after 5 days: mailed at least 5 days ago and never scanned; the
  board and manager get one My items reminder per letter to check the
  address.
* Gave up after 30 days: USPS keeps scans for 30 days, so the portal stops
  asking.

Board and manager pages only (``mail.print``). Audit events carry counts
only; nothing here logs a barcode, address or name.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record_event
from comms import usps_iv
from comms.models import (
    Mailing,
    MailingEvent,
    MailingEventKind,
    MailPiece,
    MailPieceOutcome,
    MailPieceOutcomeKind,
    MailPieceScan,
    UspsPollState,
)
from core import features
from core.policy import require
from core.scoping import system_access

logger = logging.getLogger("comms.usps")

FEATURE = "usps_tracking"
WINDOW_DAYS = 30  # USPS keeps scan data for 30 days
MISSING_AFTER_DAYS = 5
STALE_HOURS = 24
MAX_PER_RUN = 2000
# Operation codes. Without an eDoc only machine scans come back: sortation
# up to delivery point sequencing (918/919). Out-for-delivery (872, 516) and
# delivered (517) would need an eDoc, but are read if they ever appear.
SORTED_OPCODES = frozenset({"918", "919", "872", "516"})
DELIVERED_OPCODES = frozenset({"517"})
FINAL_OPCODES = SORTED_OPCODES | DELIVERED_OPCODES

SORTED_MEANING = (
    "“Sorted for delivery” means USPS sorted the letter into the carrier's route; it is "
    "usually delivered that day or the next. USPS gives actual delivery scans only to bulk "
    "mailers who file an electronic mailing statement (eDoc)."
)


def feature_on(request=None) -> bool:
    return features.enabled(FEATURE, request)


def active() -> bool:
    return feature_on() and usps_iv.config().configured


# --- Status -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    # not_mailed, not_sent, mailed, processed, sorted, delivered, missing,
    # gave_up, not_ours
    key: str
    label: str
    last_scan: datetime.datetime | None = None
    tone: str = ""


def _date(d: datetime.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def status_for(
    *,
    closing: MailingEvent | None,
    scans: list[MailPieceScan],
    outcomes: set[str],
    today: datetime.date | None = None,
) -> Status:
    """The piece's status from its mailing's closing event, its scans and
    outcomes (the 5 and 30 day rules count from the day it was mailed)."""
    today = today or timezone.localdate()
    last = max((s.scanned_at for s in scans), default=None)
    if closing is None:
        return Status("not_mailed", "Not marked as mailed", last)
    if closing.kind != MailingEventKind.MAILED or closing.mailed_on is None:
        return Status("not_sent", "Not sent", last)
    if MailPieceOutcomeKind.NOT_OURS in outcomes:
        return Status(
            "not_ours", "USPS would not show it (check the Mailer ID)", last, "tone-warning"
        )
    delivered = [s for s in scans if s.opcode in DELIVERED_OPCODES]
    if delivered:
        when = timezone.localdate(min(s.scanned_at for s in delivered))
        return Status("delivered", f"Delivered on {_date(when)}", last, "tone-success")
    sorted_ = [s for s in scans if s.opcode in SORTED_OPCODES]
    if sorted_:
        when = timezone.localdate(min(s.scanned_at for s in sorted_))
        return Status("sorted", f"Sorted for delivery on {_date(when)}", last, "tone-success")
    age = (today - closing.mailed_on).days
    if age > WINDOW_DAYS:
        return Status("gave_up", "Gave up after 30 days", last)
    if scans:
        return Status("processed", "Processed", last)
    if age >= MISSING_AFTER_DAYS:
        return Status("missing", "No scans after 5 days", last, "tone-warning")
    return Status("mailed", "Mailed", last)


def _load(pieces: list[MailPiece]) -> tuple[dict, dict]:
    ids = [p.pk for p in pieces]
    scans: dict[int, list] = {pk: [] for pk in ids}
    outcomes: dict[int, set] = {pk: set() for pk in ids}
    with system_access("usps tracking"):
        for scan in MailPieceScan.objects.filter(piece_id__in=ids):
            scans[scan.piece_id].append(scan)
        for o in MailPieceOutcome.objects.filter(piece_id__in=ids):
            outcomes[o.piece_id].add(o.kind)
    return scans, outcomes


def mailing_tracking(mailing: Mailing, letters: list[dict] | None = None) -> dict:
    """For the mailing page: one row per barcoded letter (mailing address,
    status, last scan) and the counts. Internal: the view checks
    ``mail.print`` and fetched the mailing through its scoping."""
    from comms import papermail

    with system_access("usps tracking"):
        pieces = list(MailPiece.objects.filter(mailing=mailing).order_by("group"))
        closing = MailingEvent.objects.filter(mailing=mailing).first()
    if not pieces:
        return {"rows": [], "counts": {}, "summary": ""}
    by_group = {}
    for letter in letters if letters is not None else papermail._letters(mailing):
        by_group[letter["index"]] = letter
    scans, outcomes = _load(pieces)
    rows = []
    for piece in pieces:
        status = status_for(closing=closing, scans=scans[piece.pk], outcomes=outcomes[piece.pk])
        letter = by_group.get(piece.group)
        address = ", ".join(
            line.strip() for line in (letter["address"] if letter else "").splitlines() if line
        )
        rows.append(
            {
                "piece": piece,
                "address": address or "(address no longer on file)",
                "status": status,
                "checked": MailPieceOutcomeKind.CHECKED in outcomes[piece.pk],
                "scans": len(scans[piece.pk]),
            }
        )
    counts = _counts([r["status"] for r in rows])
    return {"rows": rows, "counts": counts, "summary": summary(counts)}


def _counts(statuses: list[Status]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in statuses:
        out[s.key] = out.get(s.key, 0) + 1
    return out


def summary(counts: dict[str, int]) -> str:
    """ "5 sorted for delivery, 1 processed, 0 missing" (others when present)."""
    if not counts or set(counts) <= {"not_mailed", "not_sent"}:
        return ""
    parts = []
    if counts.get("delivered"):
        parts.append(f"{counts['delivered']} delivered")
    parts.append(f"{counts.get('sorted', 0)} sorted for delivery")
    parts.append(f"{counts.get('processed', 0)} processed")
    if counts.get("mailed"):
        parts.append(f"{counts['mailed']} not scanned yet")
    parts.append(f"{counts.get('missing', 0)} missing")
    if counts.get("gave_up"):
        parts.append(f"{counts['gave_up']} gave up after 30 days")
    if counts.get("not_ours"):
        parts.append(f"{counts['not_ours']} not shown by USPS")
    return ", ".join(parts)


def sorted_count(mailing: Mailing) -> int | None:
    """For the meeting page: letters sorted for delivery (or delivered);
    None when the mailing has no barcoded letters."""
    with system_access("usps tracking"):
        pieces = list(MailPiece.objects.filter(mailing=mailing))
        if not pieces:
            return None
        return (
            MailPieceScan.objects.filter(piece__in=pieces, opcode__in=FINAL_OPCODES)
            .values("piece_id")
            .distinct()
            .count()
        )


# --- My items: no scan after 5 days -----------------------------------------------------


def missing_pieces(today: datetime.date | None = None) -> list[dict]:
    """Barcoded letters mailed 5 to 30 days ago with no scan at all, that the
    board has not marked as checked: one My items entry each."""
    today = today or timezone.localdate()
    since = today - datetime.timedelta(days=WINDOW_DAYS)
    until = today - datetime.timedelta(days=MISSING_AFTER_DAYS)
    with system_access("usps tracking"):
        mailed = {
            e.mailing_id: e
            for e in MailingEvent.objects.filter(
                kind=MailingEventKind.MAILED, mailed_on__gte=since, mailed_on__lte=until
            )
        }
        if not mailed:
            return []
        pieces = list(
            MailPiece.objects.filter(mailing_id__in=list(mailed))
            .exclude(pk__in=MailPieceScan.objects.values("piece_id"))
            .exclude(pk__in=MailPieceOutcome.objects.values("piece_id"))
            .order_by("mailing_id", "group")
        )
    if not pieces:
        return []
    from community.models import Lot

    with system_access("usps tracking"):
        lots = {lot.pk: lot for lot in Lot.objects.filter(pk__in={p.lot_id for p in pieces})}
    return [
        {"piece": p, "lot": lots.get(p.lot_id), "mailed_on": mailed[p.mailing_id].mailed_on}
        for p in pieces
    ]


def mark_checked(piece: MailPiece, *, actor, request=None) -> MailPieceOutcome:
    """The board or manager checked the address of a letter with no scan:
    its My items reminder ends. Once per letter, append-only, audited."""
    require(actor, "mail.print")
    try:
        with transaction.atomic():
            outcome = MailPieceOutcome.objects.create(
                piece=piece, kind=MailPieceOutcomeKind.CHECKED, recorded_by=actor
            )
            record_event(
                "mailpiece.checked",
                actor=actor,
                request=request,
                target=piece,
                metadata={"mailing_id": piece.mailing_id},
            )
    except IntegrityError as exc:
        raise ValueError("This letter is already marked as checked.") from exc
    return outcome


# --- Polling ------------------------------------------------------------------------------


def eligible_pieces(today: datetime.date | None = None) -> list[MailPiece]:
    """Barcoded letters marked as mailed in the last 30 days that have not
    reached a final state (sorted, delivered, or refused by USPS)."""
    today = today or timezone.localdate()
    since = today - datetime.timedelta(days=WINDOW_DAYS)
    with system_access("usps tracking"):
        mailings = MailingEvent.objects.filter(
            kind=MailingEventKind.MAILED, mailed_on__gte=since
        ).values("mailing_id")
        done = MailPieceScan.objects.filter(opcode__in=FINAL_OPCODES).values("piece_id")
        refused = MailPieceOutcome.objects.filter(kind=MailPieceOutcomeKind.NOT_OURS).values(
            "piece_id"
        )
        return list(
            MailPiece.objects.filter(mailing_id__in=mailings)
            .exclude(pk__in=done)
            .exclude(pk__in=refused)
            .order_by("mailing_id", "group")[:MAX_PER_RUN]
        )


def imb31(piece: MailPiece) -> str:
    return piece.tracking + piece.routing


def store_scans(piece: MailPiece, scans) -> int:
    """New scans of one piece, once each (piece, time, operation code)."""
    with system_access("usps tracking"):
        seen = set(MailPieceScan.objects.filter(piece=piece).values_list("scanned_at", "opcode"))
        rows = []
        for scan in scans:
            key = (scan.scanned_at, scan.opcode)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                MailPieceScan(
                    piece=piece,
                    scanned_at=scan.scanned_at,
                    opcode=scan.opcode,
                    facility_city=scan.facility_city,
                    facility_state=scan.facility_state,
                    facility_zip=scan.facility_zip,
                    handling_event_type=scan.handling_event_type,
                    mail_phase=scan.mail_phase,
                )
            )
        if rows:
            MailPieceScan.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def _alert_auth_failure(state: UspsPollState, now) -> None:
    """One email a day to the sysadmins while USPS refuses the sign-in."""
    today = timezone.localdate(now)
    if state.auth_alerted_on == today:
        return
    from core.health import sysadmin_emails

    body = (
        "USPS refused the portal's sign-in to Informed Visibility, so paper mail scans are "
        "not being read.\n\nOn the server, run:\n  sudo bash infra/poc/set-usps.sh\n"
        "to enter the Business Customer Gateway login again (check it still has IV-MTR "
        "access). This email is sent at most once a day.\n\n"
        f"Details: {settings.PORTAL_BASE_URL}/ops/status/\n\n-- \n{settings.SITE_NAME}"
    )
    for address in sysadmin_emails():
        send_mail(
            f"[{settings.SITE_NAME}] USPS tracking: sign-in refused",
            body,
            None,
            [address],
            fail_silently=True,
        )
    state.auth_alerted_on = today


class _Stop(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _lookup(piece: MailPiece, retried: list[bool]) -> usps_iv.PieceResult:
    """One piece lookup; after a "too many requests" waits as USPS asks and
    tries once more (once per run). Anything else stops the run."""
    try:
        try:
            return usps_iv.piece_scans(imb31(piece))
        except usps_iv.UspsRateLimited as exc:
            if retried[0]:
                raise
            retried[0] = True
            usps_iv.sleep(exc.retry_after)
            return usps_iv.piece_scans(imb31(piece))
    except usps_iv.UspsRateLimited as exc:
        raise _Stop("rate_limited") from exc
    except usps_iv.UspsAuthError as exc:
        raise _Stop("auth") from exc
    except usps_iv.UspsError as exc:
        raise _Stop("network" if exc.status == 0 else "server") from exc


def poll(*, now=None) -> dict:
    """Read new scans for every eligible piece. Stops at the first refused
    sign-in or a second "too many requests"; records how it went."""
    result = {"queried": 0, "new_scans": 0, "not_found": 0, "not_ours": 0, "outcome": "off"}
    if not active():
        return result
    now = now or timezone.now()
    pieces = eligible_pieces(timezone.localdate(now))
    state = UspsPollState.get()
    state.last_attempt_at = now
    failure = ""
    retried = [False]
    for piece in pieces:
        try:
            found = _lookup(piece, retried)
        except _Stop as stop:
            failure = stop.kind
            break
        result["queried"] += 1
        if found.not_ours:
            result["not_ours"] += 1
            with system_access("usps tracking"), transaction.atomic():
                MailPieceOutcome.objects.get_or_create(
                    piece=piece, kind=MailPieceOutcomeKind.NOT_OURS
                )
            continue
        if not found.found:
            result["not_found"] += 1
            continue
        result["new_scans"] += store_scans(piece, found.scans)
    if not failure and result["queried"] and result["not_ours"] == result["queried"]:
        failure = "refused"
    result["outcome"] = failure or "ok"
    if failure:
        state.last_failure, state.last_failure_at = failure, now
        if failure == "auth":
            _alert_auth_failure(state, now)
    else:
        state.last_success_at = now
        state.last_failure = ""
    state.last_queried, state.last_new_scans = result["queried"], result["new_scans"]
    state.save()
    if result["queried"] or failure:
        record_event(
            "usps.polled",
            actor=None,
            outcome="failure" if failure else "success",
            metadata=dict(result),
        )
    logger.info(
        "comms.usps_polled",
        extra={k: result[k] for k in ("queried", "new_scans", "not_found", "not_ours", "outcome")},
    )
    return result


# --- Health check and set-up status ------------------------------------------------------


_FAILURE_WORDS = {
    "auth": "USPS refused the sign-in: a sysadmin runs infra/poc/set-usps.sh again",
    "network": "USPS could not be reached",
    "server": "USPS answered with an error",
    "rate_limited": "USPS said too many requests",
    "refused": "USPS refused every letter: check the Mailer ID and IV-MTR access",
}
IMMEDIATE_FAILURES = frozenset({"auth", "refused"})


def health() -> tuple[bool, str] | None:
    """For core.health: None while neither the feature nor a login is set."""
    cfg = usps_iv.config()
    on = feature_on()
    if not cfg.configured:
        if cfg.problem and not cfg.api_host:  # a wrong environment or host
            return False, cfg.problem
        return (True, "Not set up") if on else None
    if not on:
        return True, f"Set up ({cfg.label}); switched off in Features"
    state = UspsPollState.objects.filter(pk=1).first()
    now = timezone.now()
    failing = (
        state
        and state.last_failure
        and (state.last_success_at is None or state.last_failure_at >= state.last_success_at)
    )
    words = _FAILURE_WORDS.get(state.last_failure, "Reading scans failed") if failing else ""
    # A refused sign-in, or USPS refusing every letter, needs a person now;
    # a network or USPS hiccup only once nothing has worked for a day.
    if failing and state.last_failure in IMMEDIATE_FAILURES:
        return False, words
    last_good = state.last_success_at if state else None
    if last_good is None:
        if failing and now - state.last_failure_at > datetime.timedelta(hours=STALE_HOURS):
            return False, words
        if failing:
            return True, f"Set up ({cfg.label}); last check failed ({words}), retrying"
        return True, f"Set up ({cfg.label}); not checked yet"
    hours = int((now - last_good).total_seconds() // 3600)
    if hours > STALE_HOURS:
        return False, f"No successful check for {hours} h" + (f" ({words})" if words else "")
    if failing:
        return True, f"{cfg.label}: checked {hours} h ago; last check failed ({words}), retrying"
    return True, f"{cfg.label}: checked {hours} h ago"


def setup_status() -> dict:
    """For Paper mail settings: is tracking set up, and how is it going."""
    cfg = usps_iv.config()
    on = feature_on()
    state = UspsPollState.objects.filter(pk=1).first()
    if not cfg.configured:
        text = (
            "Not set up: a sysadmin runs sudo bash infra/poc/set-usps.sh on the server "
            "to enter the association's USPS Business Customer Gateway login."
        )
        if cfg.problem and not cfg.api_host:  # a wrong environment or host
            text = f"Not working: {cfg.problem}"
        return {"state": "not_set_up", "text": text, "feature_on": on}
    if (
        state
        and state.last_failure
        and (state.last_success_at is None or state.last_failure_at >= state.last_success_at)
    ):
        return {
            "state": "failing",
            "text": f"Connected to {cfg.label}, but the last check failed: "
            f"{_FAILURE_WORDS.get(state.last_failure, 'reading scans failed')}.",
            "feature_on": on,
        }
    text = f"Connected to {cfg.label}."
    if state and state.last_success_at:
        text += f" Last checked {timezone.localtime(state.last_success_at):%b %-d, %-I:%M %p}."
    else:
        text += " Not checked yet: scans are read every 4 hours."
    return {"state": cfg.environment, "text": text, "feature_on": on}

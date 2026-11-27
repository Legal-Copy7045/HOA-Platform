"""Whether email reached people: the outgoing record and SMTP2Go's delivery
reports (0.43.0). Pull only, like every integration (D24, D74): the worker
asks SMTP2Go's activity API every five minutes; SMTP2Go never calls the
portal.

* ``record_sent`` (called by core.email after the relay accepted a message)
  writes an append-only ``OutgoingEmail``: the bounce token, the relay and
  its message id, the kind, what it was about and the recipient's keyed
  hash. No address, subject or text.
* ``pull`` reads delivered, bounced (hard/soft), spam-complaint and
  unsubscribe events since the cursor (with a few minutes' overlap: reports
  can arrive late), stores each once (``EmailDelivery``, deduplicated, so a
  report read twice changes nothing) and feeds bounces, complaints and
  unsubscribes into the bounce handling of comms/bounces.py: the address is
  marked as bouncing for the board and the person, and a bounced official
  notice asks for a paper copy, as with the bounce mailbox. Recipient
  addresses in the reports are hashed at once and never stored or logged.
* ``status_for`` / ``summary_for`` give "Delivered / Bounced / Pending" for
  the emails about one record (ARC decisions, meeting and budget notices).
  The caller checks that the viewer may see that record.
"""

from __future__ import annotations

import datetime
import hashlib
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.scoping import system_access

logger = logging.getLogger(__name__)

PULL_MINUTES = 5
OVERLAP_MINUTES = 15  # re-read this much before the cursor (late reports; deduplicated)
FIRST_PULL_HOURS = 24
WINDOW_HOURS = 6  # at most this much activity per read
MAX_PAGES = 20
STALE_MINUTES = 60  # no successful read for this long after a failure: health fails
BURST = 5

DELIVERED, BOUNCED, PENDING = "delivered", "bounced", "pending"
STATUS_LABELS = {
    DELIVERED: "Delivered",
    BOUNCED: "Bounced",
    PENDING: "Pending",
}
STATUS_TONES = {DELIVERED: "success", BOUNCED: "danger", PENDING: "neutral"}


# --- Sending ------------------------------------------------------------------------------


def record_sent(messages, *, all_sent: bool, provider: str, now=None) -> int:
    """One OutgoingEmail per message the relay accepted. A message carries
    ``portal_ref`` etc. from core.email.tag; SMTP2Go's backend adds
    ``provider_message_id``. Returns how many were recorded."""
    from comms import bounces
    from comms.models import EmailProviderState, OutgoingEmail

    now = now or timezone.now()
    recorded, known = 0, None
    for message in messages:
        ref = getattr(message, "portal_ref", "")
        if not ref:
            continue
        accepted = hasattr(message, "provider_message_id") or (all_sent and provider != "smtp2go")
        if not accepted:
            continue
        intended = getattr(message, "portal_intended", None) or message.recipients()
        hashes = [bounces.address_hash(a) for a in intended[:50] if a]
        address_hash = hashes[0] if len(intended) == 1 and hashes else ""
        # Where it really went (the POC redirect inbox, EMAIL_REDIRECT_ALL_TO),
        # so its delivery reports match too.
        for actual in message.recipients()[:50]:
            h = bounces.address_hash(actual)
            if h not in hashes:
                hashes.append(h)
        person_id = None
        if address_hash:
            if known is None:
                known = bounces._known_addresses()
            found = known.get(address_hash)
            person_id = found[0] if found else None
        try:
            with transaction.atomic():
                OutgoingEmail.objects.create(
                    ref=ref[:64],
                    provider=provider[:10],
                    provider_message_id=(getattr(message, "provider_message_id", "") or "")[:100],
                    mail_kind=getattr(message, "portal_kind", "") or "other",
                    about=getattr(message, "portal_about", "") or "",
                    person_id=person_id,
                    address_hash=address_hash,
                    address_hashes=" ".join(hashes),
                    recipients=min(len(intended), 32767),
                    sent_at=now,
                )
        except IntegrityError:  # the same message recorded already
            continue
        recorded += 1
    if recorded:
        with transaction.atomic():
            state = EmailProviderState.get()
            state.last_send_at = now
            state.last_send_provider = provider[:10]
            state.save(update_fields=["last_send_at", "last_send_provider"])
    return recorded


def note_send_failure(code: str, now=None) -> None:
    from comms.models import EmailProviderState

    with transaction.atomic():
        state = EmailProviderState.get()
        state.last_send_failure_at = now or timezone.now()
        state.last_send_failure = (code or "")[:20]
        state.save(update_fields=["last_send_failure_at", "last_send_failure"])


# --- Reading SMTP2Go's activity ---------------------------------------------------------


def active() -> bool:
    from comms import smtp2go

    return smtp2go.selected() and smtp2go.config().configured


def _dedupe(event, address_hash: str) -> str:
    key = f"smtp2go|{event.email_id}|{event.kind}|{address_hash}|{event.at.isoformat()}"
    return hashlib.sha256(key.encode()).hexdigest()


def ingest(events, *, now=None) -> dict:
    """Store each event once and feed bounces into comms.bounces. Returns
    counts. Reading the same events again changes nothing.

    Trusted only as far as the portal's own records go (BUG-002 / EXP-001):
    the events come from SMTP2Go's API, fetched by the portal over https
    with its key, and each counts only when its email id is one the portal
    recorded for a message it sent through SMTP2Go, and its recipient (when
    named) was one of that message's recipients. Anything else is skipped."""
    from comms import bounces
    from comms.models import EmailDelivery, OutgoingEmail

    now = now or timezone.now()
    stored = new_bounces = hard = skipped = 0
    by_id: dict[str, OutgoingEmail | None] = {}
    known = None
    for event in events:
        address_hash = bounces.address_hash(event.recipient) if event.recipient else ""
        if event.email_id not in by_id:
            with system_access("delivery reports"):
                by_id[event.email_id] = (
                    OutgoingEmail.objects.filter(
                        provider="smtp2go", provider_message_id=event.email_id
                    )
                    .order_by("sent_at")
                    .first()
                )
        outgoing = by_id[event.email_id]
        if outgoing is None:
            skipped += 1  # not a message the portal sent (or sent before 0.43.0)
            continue
        if address_hash and address_hash not in (outgoing.address_hashes or "").split():
            skipped += 1  # not one of that message's recipients
            continue
        dedupe = _dedupe(event, address_hash)
        try:
            with transaction.atomic():
                EmailDelivery.objects.create(
                    outgoing=outgoing,
                    provider_message_id=event.email_id,
                    address_hash=address_hash,
                    kind=event.kind,
                    status=event.status,
                    occurred_at=event.at,
                    received_at=now,
                    dedupe=dedupe,
                )
        except IntegrityError:  # read before
            continue
        stored += 1
        full = address_hash or outgoing.address_hash
        if event.kind == "delivered" or not full:
            continue
        parsed = bounces.Parsed(
            address_hash=full,
            addr16=full[:16],
            kind=event.kind,
            status=event.status,
            mail_kind=outgoing.mail_kind,
            sent_at=outgoing.sent_at,
            dedupe=dedupe,
        )
        known = bounces._known_addresses() if known is None else known
        r, h = bounces.record([parsed], known=known, now=now)
        new_bounces += r
        hard += h
    return {"stored": stored, "bounces": new_bounces, "hard": hard, "skipped": skipped}


def pull(*, now=None) -> dict:
    """Read SMTP2Go's activity since the cursor (the periodic task)."""
    from comms import smtp2go
    from comms.models import EmailProviderState

    if not active():
        return {"state": "not_configured"}
    now = now or timezone.now()
    state = EmailProviderState.get()
    state.last_pull_attempt_at = now
    start = (
        state.cursor_at - datetime.timedelta(minutes=OVERLAP_MINUTES)
        if state.cursor_at
        else now - datetime.timedelta(hours=FIRST_PULL_HOURS)
    )
    end = min(now, start + datetime.timedelta(hours=WINDOW_HOURS))
    events, token, pages = [], "", 0
    try:
        while True:
            page, token = smtp2go.search_activity(start, end, continue_token=token, now=now)
            events.extend(page)
            pages += 1
            if not token or pages >= MAX_PAGES:
                break
    except smtp2go.Smtp2goError as exc:
        state.last_pull_failure_at = now
        state.last_pull_failure = exc.code[:20]
        state.save()
        logger.warning("delivery.pull_failed", extra={"code": exc.code})
        return {"state": "failed", "code": exc.code}
    counts = ingest(events, now=now)
    state.last_pull_success_at = now
    state.last_pull_failure = ""
    state.last_pulled = len(events)
    state.cursor_at = end
    state.save()
    if counts["hard"] >= BURST:
        from core import ops_timeline

        ops_timeline.record(
            "email",
            f"{counts['hard']} hard bounces reported by SMTP2Go in one read (addresses that "
            "do not accept mail): see People and My items",
            key="bounces",
            state="problem",
            count=counts["hard"],
        )
    if counts["stored"]:
        from audit.models import Channel
        from audit.services import record_event

        record_event(
            "comms.delivery_read",
            actor=None,
            channel=Channel.WORKER,
            metadata={
                "events": len(events),
                "stored": counts["stored"],
                "bounces": counts["bounces"],
                "hard": counts["hard"],
                "skipped": counts["skipped"],
            },
        )
    return {"state": "ok", "events": len(events), "pages": pages, **counts}


# --- Status of what was sent --------------------------------------------------------------


def _message_status(outgoing, deliveries) -> str:
    kinds = {d.kind for d in deliveries}
    if kinds & {"hard", "spam", "unsub"}:
        return BOUNCED
    if "delivered" in kinds:
        return DELIVERED
    if "soft" in kinds:
        return PENDING  # a temporary failure: the relay keeps trying
    return PENDING


def status_for(about: str) -> list[dict]:
    """Each email about one record, oldest first: when it went, its status
    and when that was reported. No addresses. The caller has checked that
    the viewer may see the record."""
    from comms.models import EmailDelivery, OutgoingEmail

    with system_access("delivery status"):
        rows = list(OutgoingEmail.objects.filter(about=about).order_by("sent_at", "id"))
        deliveries: dict[int, list] = {}
        for d in EmailDelivery.objects.filter(outgoing__in=rows).order_by("occurred_at"):
            deliveries.setdefault(d.outgoing_id, []).append(d)
    out = []
    for row in rows:
        mine = deliveries.get(row.pk, [])
        status = _message_status(row, mine)
        out.append(
            {
                "sent_at": row.sent_at,
                "status": status,
                "label": STATUS_LABELS[status],
                "tone": STATUS_TONES[status],
                "at": mine[-1].occurred_at if mine else None,
                "soft": any(d.kind == "soft" for d in mine) and status == PENDING,
                "tracked": row.provider == "smtp2go",
            }
        )
    return out


def summary_for(about: str) -> dict | None:
    """Counts for a notice sent to many: {"delivered", "bounced", "pending",
    "total", "tracked"}; None when nothing was recorded."""
    rows = status_for(about)
    if not rows:
        return None
    out = {DELIVERED: 0, BOUNCED: 0, PENDING: 0}
    for row in rows:
        out[row["status"]] += 1
    out["total"] = len(rows)
    out["tracked"] = any(r["tracked"] for r in rows)
    return out


# --- Operations status (System status, health, set-up wizard) ------------------------------


def _ago(at, now) -> str:
    minutes = int((now - at).total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def bounce_counts(now=None) -> dict:
    """Bounces in the last 7 days (counts only, no addresses)."""
    from comms.models import EmailBounce

    since = (now or timezone.now()) - datetime.timedelta(days=7)
    with system_access("bounce counts"):
        rows = EmailBounce.objects.filter(received_at__gte=since)
        return {
            "total": rows.count(),
            "hard": rows.filter(kind__in=("hard", "spam", "unsub")).count(),
        }


def overview(now=None) -> dict:
    """System status's Email card: provider, last successful send, last
    successful activity read, bounces in the last 7 days. No addresses."""
    from comms import smtp2go
    from comms.models import EmailProviderState

    now = now or timezone.now()
    state = EmailProviderState.objects.filter(pk=1).first()
    cfg = smtp2go.config()
    send_problem = ""
    if (
        state
        and state.last_send_failure
        and (state.last_send_at is None or state.last_send_failure_at >= state.last_send_at)
    ):
        send_problem = smtp2go.FAILURE_WORDS.get(
            state.last_send_failure,
            f"the mail server refused or did not answer ({state.last_send_failure}): check "
            "the EMAIL_ settings",
        )
    pull_problem = ""
    if state and state.last_pull_failure:
        pull_problem = smtp2go.FAILURE_WORDS.get(state.last_pull_failure, "reading failed")
    return {
        "provider": smtp2go.provider_label(),
        "smtp2go": smtp2go.selected(),
        "configured": cfg.configured if smtp2go.selected() else True,
        "config_problem": cfg.problem if smtp2go.selected() and not cfg.configured else "",
        "last_send_at": state.last_send_at if state else None,
        "send_problem": send_problem,
        "send_problem_at": state.last_send_failure_at if send_problem else None,
        "last_pull_at": state.last_pull_success_at if state else None,
        "pull_problem": pull_problem,
        "bounces": bounce_counts(now),
    }


def health():
    """core.health "Email delivery reports": hidden unless SMTP2Go is chosen."""
    from comms import smtp2go
    from comms.models import EmailProviderState

    if not smtp2go.selected():
        return None
    cfg = smtp2go.config()
    if not cfg.configured:
        return False, cfg.problem
    now = timezone.now()
    state = EmailProviderState.objects.filter(pk=1).first()
    if state is None or state.last_pull_attempt_at is None:
        return True, f"Set up; the first read of SMTP2Go's reports runs within {PULL_MINUTES} min"
    failing = state.last_pull_failure and (
        state.last_pull_success_at is None
        or state.last_pull_failure_at >= state.last_pull_success_at
    )
    stale = state.last_pull_success_at is None or (
        state.last_pull_success_at < now - datetime.timedelta(minutes=STALE_MINUTES)
    )
    if failing and stale:
        since = state.last_pull_success_at
        return False, (
            "Delivery and bounce reports are not being read: "
            f"{smtp2go.FAILURE_WORDS.get(state.last_pull_failure, 'reading failed')} "
            f"(last read {_ago(since, now) if since else 'never'}). On the server run "
            "manage.py check_smtp2go"
        )
    counts = bounce_counts(now)
    return True, (
        f"Last read {_ago(state.last_pull_success_at, now)}; {counts['total']} bounce"
        f"{'' if counts['total'] == 1 else 's'} in the last 7 days ({counts['hard']} that no "
        "longer accept mail)"
    )


def setup_status() -> dict:
    """For the set-up wizard's server step: never the key."""
    from comms import smtp2go

    if not smtp2go.selected():
        return {
            "state": "not_set_up",
            "provider": "smtp",
            "text": "Email goes through the SMTP server in the EMAIL_ settings. To use "
            "SMTP2Go (delivery and bounce reports without a bounce mailbox), a sysadmin runs "
            "sudo bash infra/poc/set-smtp2go.sh on the server.",
        }
    cfg = smtp2go.config()
    if not cfg.configured:
        return {
            "state": "failing",
            "provider": "smtp2go",
            "text": f"Not configured: {cfg.problem}.",
        }
    try:
        result = health()
    except Exception as exc:  # shown like a failed check
        return {
            "state": "failing",
            "provider": "smtp2go",
            "text": f"Check failed ({type(exc).__name__})",
        }
    ok, detail = result if result else (True, "Set up")
    return {
        "state": "set_up" if ok else "failing",
        "provider": "smtp2go",
        "text": f"Configured: SMTP2Go ({cfg.host}). {detail}.",
    }

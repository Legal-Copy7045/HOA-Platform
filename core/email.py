"""Email transport wrapper (decision D2).

When ``EMAIL_REDIRECT_ALL_TO`` is set (POC only), every message goes to that
one address instead, with the intended recipients noted in the subject. The
seed data uses @example.com addresses, so without this nothing would arrive.

Every message also gets a bounce token (D74, comms/bounces.py): an
``X-Portal-Ref`` header, the token inside its Message-ID and, when
``BOUNCE_RETURN_PATH`` is set, a VERP envelope sender. The kind of email
comes from an ``X-Portal-Kind`` header the caller sets (removed before
sending) or from ``with mail_kind("notice"):`` around the send.

The relay is the SMTP server in the EMAIL_* settings, or SMTP2Go's HTTP API
with ``EMAIL_PROVIDER=smtp2go`` (comms/smtp2go.py; no VERP sender then:
SMTP2Go handles bounces itself and reports them through its activity API).
Every message handed to the relay is recorded as an ``OutgoingEmail`` (the
token, the relay's message id, the kind, ``with mail_about("arc_decision",
pk):`` and the recipient's keyed hash; never the address or text), so its
delivery can be shown later (comms/delivery.py).
"""

from __future__ import annotations

import contextlib
import copy
import logging
import smtplib
from contextvars import ContextVar

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class EmailUnavailable(Exception):
    """The mail server could not be reached; shown as a friendly page."""


FAILURE_CACHE_KEY = "email:last_failure"

KIND_HEADER = "X-Portal-Kind"
REF_HEADER = "X-Portal-Ref"
NOTICE, STATEMENT, DIGEST, BOARD, ACCOUNT = "notice", "statement", "digest", "board", "account"
_kind: ContextVar[str] = ContextVar("portal_mail_kind", default="")
_about: ContextVar[str] = ContextVar("portal_mail_about", default="")


@contextlib.contextmanager
def mail_kind(kind: str):
    """Mark the emails sent inside the block (e.g. official notices, D44), so a
    bounce of one is recognised as such."""
    token = _kind.set(kind)
    try:
        yield
    finally:
        _kind.reset(token)


@contextlib.contextmanager
def mail_about(area: str, pk):
    """Mark the emails sent inside the block as being about one record (e.g.
    ``mail_about("arc_decision", decision.pk)``), so their delivery can be
    shown on that record's page."""
    token = _about.set(f"{area}:{pk}"[:40])
    try:
        yield
    finally:
        _about.reset(token)


def transport_backend() -> str:
    """The real relay's backend: SMTP2Go's API when chosen, else the
    configured one (SMTP; tests use locmem)."""
    from comms import smtp2go

    if smtp2go.selected():
        return "comms.smtp2go.Smtp2goBackend"
    return settings.EMAIL_TRANSPORT_BACKEND


def provider_name() -> str:
    from comms import smtp2go

    if smtp2go.selected():
        return smtp2go.PROVIDER
    backend = settings.EMAIL_TRANSPORT_BACKEND
    return "smtp" if backend.endswith("smtp.EmailBackend") else "other"


def tag(message):
    """A copy of ``message`` carrying its bounce token (see the module
    docstring). The original is not changed. Never fails the send."""
    from django.core.mail.message import make_msgid
    from django.core.mail.utils import DNS_NAME

    from comms import bounces

    tagged = copy.copy(message)
    headers = dict(getattr(message, "extra_headers", None) or {})
    names = {k.lower(): k for k in headers}
    kind = headers.pop(names[KIND_HEADER.lower()], "") if KIND_HEADER.lower() in names else ""
    kind = kind or _kind.get() or "other"
    ref = bounces.make_ref(kind, message.recipients())
    headers[REF_HEADER] = ref
    if "message-id" not in names:
        headers["Message-ID"] = make_msgid(idstring=ref, domain=DNS_NAME)
    from comms import smtp2go

    # SMTP2Go sets its own return path to read bounces; a VERP sender would
    # only be refused there (and by most authenticated SMTP relays too).
    envelope = "" if smtp2go.selected() else bounces.return_path(ref)
    if envelope:
        if "from" not in names:
            headers["From"] = message.from_email or settings.DEFAULT_FROM_EMAIL
        tagged.from_email = envelope
    tagged.extra_headers = headers
    tagged.portal_ref = ref
    tagged.portal_kind = kind
    tagged.portal_about = _about.get()
    tagged.portal_intended = list(message.recipients())
    return tagged


def _tagged(messages):
    out = []
    for message in messages:
        try:
            out.append(tag(message))
        except Exception as exc:  # a missing token never stops an email
            logger.warning("email.not_tagged", extra={"exc_type": type(exc).__name__})
            out.append(message)
    return out


def failure_code(exc: Exception) -> str:
    """A short code for a failed send: the relay's own code (SMTP2Go) or the
    exception type. Never its message, which can carry addresses."""
    return getattr(exc, "code", "") or type(exc).__name__


def _note_failure(exc: Exception) -> None:
    from django.core.cache import cache
    from django.utils import timezone

    code = failure_code(exc)
    # The type and code only: messages can carry addresses (CLAUDE.md logging rule).
    logger.warning("email.send_failed", extra={"exc_type": type(exc).__name__, "code": code})
    try:
        cache.set(FAILURE_CACHE_KEY, (timezone.now().isoformat(), code), 86400)
    except Exception:  # recording a failure must not fail the request
        logger.warning("email.failure_not_recorded")
    try:
        from comms import delivery

        delivery.note_send_failure(code)
    except Exception:
        logger.warning("email.failure_not_recorded")


def _note_recovery() -> None:
    """A message went out, so mail works again: clear any remembered failure
    (System status "Outgoing email" recovers at once, not a day later)."""
    from django.core.cache import cache

    try:
        cache.delete(FAILURE_CACHE_KEY)
    except Exception:  # clearing must not fail the send
        logger.warning("email.recovery_not_recorded")


def clear_failure() -> None:
    _note_recovery()


class RedirectingBackend(BaseEmailBackend):
    def __init__(self, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self._inner = get_connection(transport_backend(), fail_silently=fail_silently, **kwargs)

    def open(self):
        return self._inner.open()

    def close(self):
        return self._inner.close()

    def send_messages(self, email_messages):
        from comms.smtp2go import Smtp2goError

        try:
            sent = self._send(email_messages)
        except (OSError, smtplib.SMTPException, Smtp2goError) as exc:
            _note_failure(exc)
            if self.fail_silently:
                return 0
            raise EmailUnavailable from exc
        if sent:
            _note_recovery()
        return sent

    def _send(self, email_messages):
        email_messages = _tagged(email_messages)
        target = settings.EMAIL_REDIRECT_ALL_TO
        if not target:
            sent = self._inner.send_messages(email_messages)
            _record(email_messages, sent)
            return sent
        redirected = []
        for message in email_messages:
            copy_ = copy.copy(message)
            intended = ", ".join(message.to + message.cc + message.bcc)
            copy_.subject = f"[to: {intended}] {message.subject}"
            copy_.to, copy_.cc, copy_.bcc = [target], [], []
            redirected.append(copy_)
        sent = self._inner.send_messages(redirected)
        _record(redirected, sent)
        return sent


def _record(messages, sent: int) -> None:
    """Record what the relay accepted (comms.delivery). Never fails a send."""
    if not sent:
        return
    try:
        from comms import delivery

        delivery.record_sent(messages, all_sent=sent >= len(messages), provider=provider_name())
    except Exception as exc:  # the email went; only its record is missing
        logger.warning("email.not_recorded", extra={"exc_type": type(exc).__name__})

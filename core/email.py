"""Email transport wrapper (decision D2).

When ``EMAIL_REDIRECT_ALL_TO`` is set (POC only), every message goes to that
one address instead, with the intended recipients noted in the subject. The
seed data uses @example.com addresses, so without this nothing would arrive.
"""

from __future__ import annotations

import copy
import logging
import smtplib

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class EmailUnavailable(Exception):
    """The mail server could not be reached; shown as a friendly page."""


FAILURE_CACHE_KEY = "email:last_failure"


def _note_failure(exc: Exception) -> None:
    from django.core.cache import cache
    from django.utils import timezone

    # The exception type only: messages can carry addresses (CLAUDE.md logging rule).
    logger.warning("email.send_failed", extra={"exc_type": type(exc).__name__})
    try:
        cache.set(FAILURE_CACHE_KEY, (timezone.now().isoformat(), type(exc).__name__), 86400)
    except Exception:  # recording a failure must not fail the request
        logger.warning("email.failure_not_recorded")


class RedirectingBackend(BaseEmailBackend):
    def __init__(self, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self._inner = get_connection(
            settings.EMAIL_TRANSPORT_BACKEND, fail_silently=fail_silently, **kwargs
        )

    def open(self):
        return self._inner.open()

    def close(self):
        return self._inner.close()

    def send_messages(self, email_messages):
        try:
            return self._send(email_messages)
        except (OSError, smtplib.SMTPException) as exc:
            _note_failure(exc)
            if self.fail_silently:
                return 0
            raise EmailUnavailable from exc

    def _send(self, email_messages):
        target = settings.EMAIL_REDIRECT_ALL_TO
        if not target:
            return self._inner.send_messages(email_messages)
        redirected = []
        for message in email_messages:
            copy_ = copy.copy(message)
            intended = ", ".join(message.to + message.cc + message.bcc)
            copy_.subject = f"[to: {intended}] {message.subject}"
            copy_.to, copy_.cc, copy_.bcc = [target], [], []
            redirected.append(copy_)
        return self._inner.send_messages(redirected)

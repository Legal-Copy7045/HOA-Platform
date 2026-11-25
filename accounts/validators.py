"""Breached-password check against Have I Been Pwned (decision D3).

k-anonymity: only the first 5 hex characters of the password's SHA-1 leave
the server; the comparison happens here. Fail-open: if the API cannot be
reached, the password is accepted (other validators and mandatory MFA still
apply) and a warning without personal data is logged.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request

from django.conf import settings
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)
API = "https://api.pwnedpasswords.com/range/"


def breach_count(password: str) -> int | None:
    """Times the password appears in known breaches; None if unknown."""
    digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        API + prefix, headers={"Add-Padding": "true", "User-Agent": "hoa-portal"}
    )
    try:
        with urllib.request.urlopen(  # nosec B310  # noqa: S310
            request, timeout=settings.PASSWORD_BREACH_CHECK_TIMEOUT
        ) as response:
            body = response.read().decode("ascii", errors="replace")
    except Exception:
        logger.warning("password.breach_check_unavailable")
        return None
    for line in body.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip() == suffix:
            try:
                return int(count.strip())
            except ValueError:
                return 0
    return 0


class BreachedPasswordValidator:
    def validate(self, password: str, user=None) -> None:
        if not settings.PASSWORD_BREACH_CHECK:
            return
        count = breach_count(password)
        if count:
            raise ValidationError(
                "This password has appeared in a data breach and could be guessed. "
                "Please choose a different one.",
                code="password_breached",
            )

    def get_help_text(self) -> str:
        return "Your password must not appear in known data breaches."

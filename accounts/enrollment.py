"""Where a login with no passkey or authenticator app may enroll one.

Only in the browser session that just accepted an invitation or redeemed an
assisted-reset code, and only for a short while (security review 2026-11,
finding B2). Otherwise anyone who learned the password, or could read the
mailbox and reset it, could sign in and enroll *their* authenticator: email
would effectively be the only factor, which D5 forbids.
"""

from __future__ import annotations

import time

from django.conf import settings

SESSION_KEY = "mfa_enrollment_until"


def grant(request) -> None:
    request.session[SESSION_KEY] = int(time.time()) + settings.MFA_ENROLLMENT_WINDOW_SECONDS


def allowed(request) -> bool:
    try:
        until = int(request.session.get(SESSION_KEY) or 0)
    except (TypeError, ValueError):
        return False
    return until > time.time()


def clear(request) -> None:
    request.session.pop(SESSION_KEY, None)

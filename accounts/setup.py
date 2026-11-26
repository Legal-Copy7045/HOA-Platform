"""Guided sign-in setup after an invitation or an assisted reset (UX review #2).

Three steps: 1. choose a password (or enter the reset code), 2. add a passkey
or an authenticator app, 3. save the recovery codes. While they are under way
the sidebar is hidden, so the owner cannot wander off half-done.

This only decides what the pages *show*. Who may enroll, and when, is still
decided by ``accounts.middleware.RequireMFAMiddleware`` and
``accounts.enrollment`` (D5, security review B2); nothing here opens a page.
"""

from __future__ import annotations

from django.urls import Resolver404, resolve

from accounts import enrollment
from accounts.auth import has_strong_authenticator

# Pages that belong to the setup steps. After step 2 the sidebar comes back on
# any other page, so an owner who wanders off is not left without a menu.
SETUP_URL_NAMES = frozenset(
    {
        "mfa_index",
        "mfa_activate_totp",
        "mfa_add_webauthn",
        "mfa_view_recovery_codes",
        "mfa_generate_recovery_codes",
        "mfa_reauthenticate",
        "account_reauthenticate",
        "account_setup_done",
    }
)

STEP_LABELS = {
    "invite": ("Choose a password", "Add a passkey or app", "Save recovery codes"),
    "reset": ("Enter your code", "Add a passkey or app", "Save recovery codes"),
    "recovery": ("Confirm recovery", "Add a passkey or app", "Save recovery codes"),
}


def _url_name(request) -> str:
    try:
        return resolve(request.path_info).url_name or ""
    except Resolver404:
        return ""


def state(request) -> dict | None:
    """The setup step this request is on, or None when setup is not under way."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    name = _url_name(request)
    if name == "enroll_with_code":
        step, via = 1, "reset"
    elif not has_strong_authenticator(user):
        step, via = 2, enrollment.via(request)
    elif enrollment.allowed(request) and name in SETUP_URL_NAMES:
        step, via = 3, enrollment.via(request)
    else:
        return None
    labels = STEP_LABELS[via]
    return {
        "step": step,
        "steps": [
            {"number": n, "label": label, "done": n < step, "current": n == step}
            for n, label in enumerate(labels, start=1)
        ],
    }

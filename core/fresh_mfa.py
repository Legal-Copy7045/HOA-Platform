"""A fresh second factor for the most sensitive steps (owner's decision,
D93): saving or removing a key on Keys and connections, showing a key's last
four characters, and a board member's approval or refusal of a Stripe or
Teller change.

Fresh means allauth recorded a second-factor use in this session within the
last ``WINDOW_SECONDS`` (5 minutes): a passkey (WebAuthn), or an
authenticator-app code for a login that has no passkey yet. A password alone,
an emailed code (allauth's "code" method) and a recovery code never count.
Every save, approval and reveal checks again, so after the window the person
is asked again. The prompt is allauth's own MFA re-authentication page
(``mfa_reauthenticate_webauthn`` for a passkey, ``mfa_reauthenticate`` for the
app).

Never while demo-switched (D97; review fixes 0.45.0): the session's second
factor is the real user's, not the persona's, so nothing here is fresh while
a board member views the POC as a persona, and :func:`required` refuses
outright instead of asking."""

from __future__ import annotations

import time
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse

WINDOW_SECONDS = 5 * 60
SESSION_KEY = "account_authentication_methods"
PASSKEY, APP = "webauthn", "totp"


def has_passkey(user) -> bool:
    from allauth.mfa.models import Authenticator

    return Authenticator.objects.filter(user=user, type=Authenticator.Type.WEBAUTHN).exists()


def accepted_types(user) -> frozenset[str]:
    """A passkey when the login has one (phishing-resistant); otherwise an
    authenticator-app code until a passkey is added."""
    return frozenset({PASSKEY}) if has_passkey(user) else frozenset({PASSKEY, APP})


def last_factor_at(request) -> float | None:
    """When an accepted second factor was last used in this session."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    wanted = accepted_types(user)
    best = None
    for entry in request.session.get(SESSION_KEY) or []:
        if not isinstance(entry, dict) or entry.get("method") != "mfa":
            continue
        if entry.get("type") not in wanted:
            continue
        try:
            at = float(entry.get("at"))
        except (TypeError, ValueError):
            continue
        best = at if best is None else max(best, at)
    return best


def _switched(request) -> bool:
    from core import demo_mode

    return demo_mode.switched(request)


def is_fresh(request, now: float | None = None) -> bool:
    if _switched(request):
        return False
    at = last_factor_at(request)
    if at is None:
        return False
    now = time.time() if now is None else now
    return 0 <= now - at < WINDOW_SECONDS


def prompt_url(request, next_url: str) -> str:
    name = "mfa_reauthenticate_webauthn" if has_passkey(request.user) else "mfa_reauthenticate"
    return f"{reverse(name)}?{urlencode({'next': next_url})}"


def required(view):
    """Decorator: GET and POST need a fresh second factor (after the view's
    own permission check, which the caller does first)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if _switched(request):
            from core import demo_mode

            raise PermissionDenied(demo_mode.SWITCHED_MESSAGE)
        if not is_fresh(request):
            if request.method == "POST":
                messages.info(
                    request,
                    "Confirm it's you again with your "
                    + ("passkey" if has_passkey(request.user) else "authenticator app")
                    + ": nothing was saved. The check lasts 5 minutes.",
                )
            return redirect(prompt_url(request, request.get_full_path()))
        return view(request, *args, **kwargs)

    return wrapper

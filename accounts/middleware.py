"""Strong authentication is mandatory for every account (decision D5).

A signed-in user with no TOTP or passkey can reach only the pages needed to
enroll one (and to sign out). This applies to owners, board, ARC, auditors
and sysadmins alike, and is checked on every request, so a removed
authenticator takes effect immediately.
"""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from accounts import enrollment
from accounts.auth import has_strong_authenticator

# Pages a not-yet-enrolled user may open.
ALLOWED_URL_NAMES = {
    "account_logout",
    "account_reauthenticate",
    "account_login",
    "account_change_password",
    "healthz",
    "enroll_with_code",  # assisted reset (step 7)
}


# Reachable even when the enrollment window has expired.
EXPIRED_ALLOWED_URL_NAMES = {"account_logout", "healthz", "account_setup_expired"}

# While an approved assisted reset waits for its code, only these work.
RESET_ALLOWED_URL_NAMES = {"enroll_with_code", "account_logout", "healthz"}


def _url_name(request) -> str | None:
    try:
        return resolve(request.path_info).url_name or ""
    except Resolver404:
        return None


def _is_static(request) -> bool:
    return request.path_info.startswith("/" + settings.STATIC_URL.lstrip("/"))


def _is_allowed(request) -> bool:
    if _is_static(request):
        return True
    name = _url_name(request)
    if name is None:
        return False
    return name.startswith("mfa_") or name in ALLOWED_URL_NAMES


class RequireMFAMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        from accounts.mfa_reset import open_reset_for

        if (
            user is not None
            and user.is_authenticated
            and not _is_static(request)
            and open_reset_for(user)
            and _url_name(request) not in RESET_ALLOWED_URL_NAMES
        ):
            return redirect("enroll_with_code")
        if (
            user is not None
            and user.is_authenticated
            and not _is_static(request)
            and _url_name(request) not in EXPIRED_ALLOWED_URL_NAMES
            and not has_strong_authenticator(user)
            and not enrollment.allowed(request)
            and not open_reset_for(user)
        ):
            # No authenticator and no enrollment window in this session: a
            # password (or emailed reset) alone never opens the account.
            from django.contrib.auth import logout

            from audit.services import record_event

            # Audited (D59): the board can see who keeps getting stuck here.
            record_event(
                "auth.setup_window_closed",
                request=request,
                actor=user,
                target=user,
                metadata={
                    "reason": "expired" if request.session.get(enrollment.SESSION_KEY) else "none"
                },
            )
            logout(request)
            return redirect("account_setup_expired")
        if (
            user is not None
            and user.is_authenticated
            and not _is_allowed(request)
            and not has_strong_authenticator(user)
        ):
            messages.info(
                request,
                "Set up a passkey or an authenticator app to continue. Every account needs one.",
            )
            return redirect("mfa_index")
        return self.get_response(request)

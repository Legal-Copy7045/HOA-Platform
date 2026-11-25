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


def _is_allowed(request) -> bool:
    if request.path_info.startswith("/" + settings.STATIC_URL.lstrip("/")):
        return True
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return False
    name = match.url_name or ""
    return name.startswith("mfa_") or name in ALLOWED_URL_NAMES


class RequireMFAMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
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

"""Forgot password: the emailed link, then the second step, then a new password (D58).

allauth's reset link proves only the mailbox. Before the new-password form is
shown, the person must pass their passkey, authenticator app or a recovery
code, in this browser session; nobody is signed in by the reset
(ACCOUNT_LOGIN_ON_PASSWORD_RESET stays False), and afterwards every address
on file gets "Your password was changed".
"""

from __future__ import annotations

import time

from allauth.account.views import PasswordResetFromKeyView
from allauth.mfa.base.forms import AuthenticateForm
from allauth.mfa.models import Authenticator
from allauth.mfa.webauthn.forms import AuthenticateWebAuthnForm
from django.conf import settings
from django.shortcuts import render

from accounts.auth import has_strong_authenticator
from audit.models import Outcome
from audit.services import record_event

SESSION_KEY = "password_reset_second_step"
# How long a passed second step keeps the new-password form open.
VERIFIED_FOR_SECONDS = 600


def _verified(request, user) -> bool:
    data = request.session.get(SESSION_KEY) or {}
    return data.get("user") == user.pk and float(data.get("until") or 0) > time.time()


def _mark_verified(request, user) -> None:
    request.session[SESSION_KEY] = {"user": user.pk, "until": time.time() + VERIFIED_FOR_SECONDS}


class ResetFromKeyView(PasswordResetFromKeyView):
    """allauth's view with the second step in front of the password form."""

    def _second_step(self, request):
        user = self.reset_user
        if not has_strong_authenticator(user):
            return render(
                request,
                "account/password_reset_no_mfa.html",
                status=403,
            )
        code_form = AuthenticateForm(user=user)
        passkey_form = AuthenticateWebAuthnForm(user=user)
        error = None
        if request.method == "POST" and request.POST.get("step") == "verify":
            if "code" in request.POST:
                posted = code_form = AuthenticateForm(user=user, data=request.POST)
            else:
                posted = passkey_form = AuthenticateWebAuthnForm(user=user, data=request.POST)
            if posted.is_valid():
                authenticator = posted.cleaned_data.get("credential") or posted.authenticator
                authenticator.record_usage()
                _mark_verified(request, user)
                record_event(
                    "password.reset_verified",
                    request=request,
                    target=user,
                    metadata={"type": authenticator.type},
                )
                from accounts.recovery import cancel_on_sign_in

                # Proof of the passkey or app also stops a pending recovery.
                cancel_on_sign_in(user, request=request)
                return None
            record_event(
                "password.reset_verify_failed",
                request=request,
                target=user,
                outcome=Outcome.FAILURE,
            )
            error = "That code is not right. Try again, or use a recovery code."
        context = {
            "form": code_form,
            "webauthn_form": passkey_form,
            "error": error,
            "has_passkey": Authenticator.objects.filter(
                user=user, type=Authenticator.Type.WEBAUTHN
            ).exists(),
            "action_url": request.path,
        }
        if context["has_passkey"]:
            from allauth.mfa.webauthn.internal.flows import auth as webauthn_auth

            context["js_data"] = {"request_options": webauthn_auth.begin_authentication(user)}
        return render(request, "account/password_reset_verify.html", context)

    def get(self, request, *args, **kwargs):
        if not _verified(request, self.reset_user):
            return self._second_step(request)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if not _verified(request, self.reset_user):
            response = self._second_step(request)
            if response is not None:
                return response
            # Passed: show the new-password form (a GET of the same page).
            return super().get(request, *args, **kwargs)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = self.reset_user
        response = super().form_valid(form)
        self.request.session.pop(SESSION_KEY, None)
        _password_changed_notice(user)
        return response


def _password_changed_notice(user) -> None:
    from comms.notify import send_now
    from community.services import recovery_contacts

    emails, _number = recovery_contacts(user)
    send_now(
        emails,
        subject=f"[{settings.SITE_NAME}] Your password was changed",
        body=(
            "The password for your account was just changed with an emailed reset link and "
            "your passkey or authenticator app.\n\nSign in with the new password at "
            f"{settings.PORTAL_BASE_URL}/.\n\nIf this wasn't you, call the board at once.\n\n"
            f"-- \n{settings.SITE_NAME}"
        ),
    )


password_reset_from_key = ResetFromKeyView.as_view()

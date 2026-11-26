"""Can't sign in? (decision D62): one guided entry point on the sign-in page.

It asks what's wrong and routes to the flows that already exist; none of the
checks behind them change. Every page here is a sign-in step (no sign-in
needed), shows nothing about any account, and reads only settings and the
board's sign-in help contact. The one form that acts ("Ask the board for a
new setup link") answers every address the same way and does the same work
for each (see accounts/setup_requests.py).
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from accounts.models import User

# (value, label, URL name): the order they are offered in.
PROBLEMS = (
    ("password", "I forgot my password", "account_reset_password"),
    (
        "device",
        "I lost my phone, or my passkey or authenticator app isn't working",
        "signin_help_device",
    ),
    (
        "setup",
        "My setup link expired, or I never finished setting up",
        "signin_help_setup",
    ),
    ("email", "I can't get into my email any more", "signin_help_email"),
    (
        "locked",
        "It says I'm locked out or there were too many attempts",
        "signin_help_locked",
    ),
)
ROUTES = {value: url_name for value, _label, url_name in PROBLEMS}

RECOVERY_CODE_NOTE = (
    "Sign in with your email and password as usual. When it asks for the 6-digit "
    "code, type one of your recovery codes instead (each one works once). Or, on "
    'the other phone or computer that has your passkey, choose "Sign in with a '
    'passkey". Then add a new passkey or app under Sign-in and security.'
)


def _choose(request, *, param: str, allowed, template: str, context: dict):
    """A GET form of radio buttons: no choice yet shows it, a known one
    returns it, anything else shows it again with an error."""
    choice = request.GET.get(param)
    if choice is None:
        return None, render(request, template, context)
    if choice in allowed:
        return choice, None
    return None, render(request, template, {**context, "error": "Choose one of the options."})


@never_cache
@require_GET
def signin_help(request):
    """Can't sign in? What's wrong?"""
    choice, page = _choose(
        request,
        param="problem",
        allowed=ROUTES,
        template="accounts/signin_help.html",
        context={"problems": [(value, label) for value, label, _ in PROBLEMS]},
    )
    if page is not None:
        return page
    return redirect(ROUTES[choice])


@never_cache
@require_GET
def signin_help_device(request):
    """Lost phone or passkey: a recovery code or another device, or recovery."""
    choice, page = _choose(
        request,
        param="have",
        allowed=("yes", "no"),
        template="accounts/signin_help_device.html",
        context={},
    )
    if page is not None:
        return page
    if choice == "no":
        return redirect("recovery_request")
    messages.info(request, RECOVERY_CODE_NOTE)
    return redirect("account_login")


class SetupLinkRequestForm(forms.Form):
    email = forms.EmailField(label="Your email address", max_length=254)


def _defer_request(user_id: int) -> None:
    """The same work whatever the address: a queued job carrying only a login
    id (0 when unknown); the worker decides whether the board is told."""
    from django.db import transaction

    from accounts.tasks import record_setup_link_request

    transaction.on_commit(lambda: record_setup_link_request.defer(user_id=user_id))


@never_cache
@require_http_methods(["GET", "POST"])
def signin_help_setup(request):
    """Setup link expired or never finished: ask the board for a new one."""
    from allauth.core import ratelimit

    if request.user.is_authenticated:
        return redirect("home")
    form = SetupLinkRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        limited = ratelimit.consume_or_429(request, action="setup_link_request", key=email)
        if limited:
            return limited
        user_id = User.objects.filter(email=email, is_active=True).values_list("pk", flat=True)
        _defer_request(user_id.first() or 0)
        return render(request, "accounts/signin_help_setup_sent.html")
    return render(request, "accounts/signin_help_setup.html", {"form": form})


@never_cache
@require_GET
def signin_help_email(request):
    """No access to the email any more: only the board can help."""
    return render(request, "accounts/signin_help_email.html")


@never_cache
@require_GET
def signin_help_locked(request):
    """Locked out or too many attempts: how long to wait."""
    from accounts.templatetags.signin import login_pauses

    return render(request, "accounts/signin_help_locked.html", {"pauses": login_pauses()})

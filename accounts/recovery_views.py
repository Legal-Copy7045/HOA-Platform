"""Pages of the self-service account recovery (D58). See accounts/recovery.py.

The three pages before sign-in are sign-in steps (no sign-in needed): the
email form, the emailed link (swapped for a session marker, so the token
leaves the URL bar) and the page that confirms the start or finishes it.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from accounts import recovery
from accounts.models import AccountRecovery, RecoveryCancelledBy, RecoveryLinkPurpose, User
from core import policy

INVALID = "accounts/recovery_invalid.html"


class RecoveryRequestForm(forms.Form):
    email = forms.EmailField(label="Your email address", max_length=254)


def _defer_link(user_id: int) -> None:
    """The same work whatever the address: one lookup, then a queued job
    that decides (in the worker) whether anything is emailed."""
    from django.db import transaction

    from accounts.tasks import send_recovery_link

    transaction.on_commit(lambda: send_recovery_link.defer(user_id=user_id))


@never_cache
@require_http_methods(["GET", "POST"])
def recovery_request(request):
    """ "I've lost my phone and recovery codes": always the same answer."""
    from allauth.core import ratelimit

    if request.user.is_authenticated:
        return redirect("account_recovery_status")
    form = RecoveryRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        limited = ratelimit.consume_or_429(request, action="account_recovery", key=email)
        if limited:
            return limited
        user_id = User.objects.filter(email=email, is_active=True).values_list("pk", flat=True)
        _defer_link(user_id.first() or 0)
        return render(request, "accounts/recovery_sent.html")
    return render(
        request,
        "accounts/recovery_request.html",
        {"form": form, "wait": recovery.wait_hours(), "lock": recovery.lock_hours()},
    )


@never_cache
@require_GET
def recovery_link(request, token):
    if request.user.is_authenticated:
        return render(request, INVALID, {"signed_in": True}, status=400)
    link = recovery.find_link(token)
    if link is None:
        return render(request, INVALID, status=404)
    request.session.cycle_key()
    request.session[recovery.SESSION_LINK] = link.pk
    request.session[recovery.SESSION_TOKEN] = recovery._hash(token)
    return redirect("recovery_continue")


@never_cache
@require_http_methods(["GET", "POST"])
def recovery_continue(request):
    """Confirm the start, or (after the wait, in this browser session) finish."""
    if request.user.is_authenticated:
        return render(request, INVALID, {"signed_in": True}, status=400)
    link = recovery.link_from_session(request)
    if link is None:
        recovery.forget_session(request)
        return render(request, INVALID, status=404)
    error = None
    if link.purpose == RecoveryLinkPurpose.START:
        if request.method == "POST":
            try:
                started = recovery.start(link, request=request)
            except recovery.RecoveryError as exc:
                recovery.forget_session(request)
                return render(request, INVALID, {"message": str(exc)}, status=400)
            recovery.forget_session(request)
            return render(
                request,
                "accounts/recovery_started.html",
                {"ready": recovery.when(started.ready_at), "wait": started.wait_hours},
            )
        return render(
            request,
            "accounts/recovery_confirm.html",
            {"wait": recovery.wait_hours(), "lock": recovery.lock_hours()},
        )
    current = link.recovery
    if request.method == "POST":
        try:
            recovery.complete(link, request=request)
        except recovery.RecoveryError as exc:
            error = str(exc)
        else:
            messages.success(
                request, "Your old sign-in methods are removed. Now add a passkey or app."
            )
            return redirect("mfa_index")
    return render(
        request,
        "accounts/recovery_finish.html",
        {
            "recovery": current,
            "ready": recovery.when(current.ready_at),
            "lock_hours": current.lock_hours,
            "error": error,
        },
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def account_recovery_status(request):
    """The page every notice links to: the person sees and cancels a recovery
    of their own account (signing in normally already cancels it)."""
    mine = AccountRecovery.objects.unscoped("own recoveries").filter(subject=request.user)
    current = recovery.open_recovery(request.user)
    if request.method == "POST":
        if current is not None:
            try:
                recovery.cancel(
                    current, actor=request.user, by=RecoveryCancelledBy.PERSON, request=request
                )
            except recovery.RecoveryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Cancelled. Nothing was changed on your account.")
        return redirect("account_recovery_status")
    return render(
        request,
        "accounts/recovery_status.html",
        {
            "current": current,
            "ready": recovery.when(current.ready_at) if current else "",
            "past": list(mine[:5]),
            "locked_until": recovery.locked_until(request.user),
        },
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def recovery_board(request, pk):
    """Board: one recovery request, with Cancel while it waits."""
    policy.require(request.user, "account_recovery.cancel")
    row = get_object_or_404(
        AccountRecovery.objects.unscoped("board recovery page").select_related("subject"), pk=pk
    )
    if request.method == "POST":
        try:
            recovery.cancel(row, actor=request.user, by=RecoveryCancelledBy.BOARD, request=request)
        except recovery.RecoveryError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Recovery cancelled. The person was told by email.")
        return redirect("recovery_board", pk=row.pk)
    from community.services import person_and_homes

    name, homes = person_and_homes(row.subject)
    return render(
        request,
        "accounts/board/recovery.html",
        {
            "row": row,
            "name": name,
            "homes": homes,
            "started": recovery.when(row.started_at),
            "ready": recovery.when(row.ready_at),
            "lock_until": recovery.when(row.lock_until) if row.lock_until else "",
        },
    )

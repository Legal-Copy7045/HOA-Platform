"""Preview as owner: a board member or manager sees the portal exactly as the
owner of one lot sees it, for testing and for helping owners.

- No new data: these roles can already see every lot (lot.view_all).
- Read-only: while previewing, anything other than GET/HEAD is refused
  (except leaving the preview and signing out).
- The person stays the viewer: profile, ballots and personal pages are the
  viewer's own; only the *permissions and lot scope* become the owner's.
- Audited when it starts and stops; a banner shows on every page.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import Resolver404, resolve
from django.views.decorators.http import require_POST

from audit.services import record_event

SESSION_KEY = "owner_preview_lot"


@dataclass(frozen=True)
class Preview:
    user_id: int
    lot_id: int
    lot_number: int


_current: contextvars.ContextVar[Preview | None] = contextvars.ContextVar(
    "owner_preview", default=None
)


def current_for(user) -> Preview | None:
    preview = _current.get()
    if preview is not None and getattr(user, "pk", None) == preview.user_id:
        return preview
    return None


# Reachable while previewing even though they change something.
ALLOWED_POST_URL_NAMES = {"owner_preview_stop", "account_logout"}


class OwnerPreviewMiddleware:
    """After authentication: switches roles to the previewed lot's owner view
    and refuses changes. Must run before anything that checks permissions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        lot_id = request.session.get(SESSION_KEY) if hasattr(request, "session") else None
        if not lot_id or user is None or not user.is_authenticated:
            return self.get_response(request)
        from community.models import Lot
        from core import policy
        from core.scoping import system_access

        # Re-check the right to preview on every request, as the real user.
        if not policy.can(user, "portal.preview_as_owner"):
            request.session.pop(SESSION_KEY, None)
            return self.get_response(request)
        with system_access("owner preview"):
            lot = Lot.objects.filter(pk=lot_id).first()
        if lot is None:
            request.session.pop(SESSION_KEY, None)
            return self.get_response(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                name = resolve(request.path_info).url_name
            except Resolver404:
                name = None
            if name not in ALLOWED_POST_URL_NAMES:
                return HttpResponseForbidden(
                    "Preview as owner is read-only. Exit the preview to make changes.",
                    content_type="text/plain",
                )
        request.owner_preview = Preview(user.pk, lot.pk, lot.number)
        token = _current.set(request.owner_preview)
        try:
            return self.get_response(request)
        finally:
            _current.reset(token)


@login_required
@require_POST
def start(request):
    from community.models import Lot
    from core import policy

    policy.require(request.user, "portal.preview_as_owner")
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=request.POST.get("lot"))
    request.session[SESSION_KEY] = lot.pk
    record_event(
        "preview.owner_started",
        actor=request.user,
        request=request,
        target=lot,
        metadata={"lot_id": lot.pk},
    )
    messages.info(request, f"You're seeing the portal as the owner of lot {lot.number} sees it.")
    return redirect("home")


@login_required
@require_POST
def stop(request):
    lot_id = request.session.pop(SESSION_KEY, None)
    if not lot_id:
        return redirect("home")
    record_event(
        "preview.owner_stopped",
        actor=request.user,
        request=request,
        metadata={"lot_id": lot_id},
    )
    return redirect("lot_detail", lot_id)  # where "Preview as owner" was started

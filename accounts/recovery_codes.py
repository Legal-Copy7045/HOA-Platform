"""Recovery codes: shown once, then printed or downloaded from that showing (D58).

A new set of codes is shown in the browser session that first opens it; that
session may print it or download it as a text file for a while. Any other
session (or later) sees that the codes are hidden and can make new ones (the
old ones stop working). Each showing, print and download is audited
("mfa.recovery_codes_viewed", with how); the codes themselves are never
logged or audited.
"""

from __future__ import annotations

import time

from allauth.account.decorators import reauthentication_required
from allauth.mfa import app_settings
from allauth.mfa.models import Authenticator
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache

from audit.services import record_event

SESSION_KEY = "recovery_codes_shown"
# How long the session that first opened a set may print or download it.
SHOWN_FOR_SECONDS = 1800


def _codes(user):
    authenticator = Authenticator.objects.filter(
        user=user, type=Authenticator.Type.RECOVERY_CODES
    ).first()
    return (authenticator, authenticator.wrap()) if authenticator else (None, None)


def _may_see(request, authenticator, wrapper) -> bool:
    """First showing of this set in any session, or the session that had it."""
    grant = request.session.get(SESSION_KEY) or {}
    if grant.get("id") == authenticator.pk and float(grant.get("until") or 0) > time.time():
        return True
    if wrapper.did_view:
        return False
    wrapper.mark_as_viewed()
    request.session[SESSION_KEY] = {
        "id": authenticator.pk,
        "until": time.time() + SHOWN_FOR_SECONDS,
    }
    return True


def _audit(request, how: str) -> None:
    record_event(
        "mfa.recovery_codes_viewed", request=request, target=request.user, metadata={"how": how}
    )


@login_required
@never_cache
@reauthentication_required(app="allauth.mfa")
def view_codes(request):
    authenticator, wrapper = _codes(request.user)
    if authenticator is None:
        raise Http404
    can_view = _may_see(request, authenticator, wrapper)
    unused = wrapper.get_unused_codes()
    if can_view:
        _audit(request, "page")
    else:
        unused = ["****" for _ in unused]
    return render(
        request,
        "mfa/recovery_codes/index.html",
        {
            "unused_codes": unused,
            "unused_count": len(unused),
            "can_view_codes": can_view,
            "can_download_codes": can_view and bool(unused),
            "can_generate_codes": True,
            "total_count": app_settings.RECOVERY_CODE_COUNT,
        },
    )


def _granted(request):
    authenticator, wrapper = _codes(request.user)
    grant = request.session.get(SESSION_KEY) or {}
    if (
        authenticator is None
        or grant.get("id") != authenticator.pk
        or float(grant.get("until") or 0) <= time.time()
    ):
        raise Http404
    unused = wrapper.get_unused_codes()
    if not unused:
        raise Http404
    return unused


@login_required
@never_cache
def download_codes(request):
    unused = _granted(request)
    _audit(request, "download")
    body = render_to_string("mfa/recovery_codes/download.txt", {"unused_codes": unused})
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="recovery-codes.txt"'
    return response


@login_required
@never_cache
def print_codes(request):
    unused = _granted(request)
    _audit(request, "print")
    return render(request, "mfa/recovery_codes/print.html", {"unused_codes": unused})

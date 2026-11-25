"""Installable app (manifest, service worker), push and text alert settings,
and the board's urgent text notice page (Milestone 4).

Every page needs sign-in (D24), the manifest and service worker included:
they hold nothing personal, but nothing is served to anonymous visitors
except the sign-in steps.
"""

from __future__ import annotations

import hashlib
import json

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.views.decorators.http import require_POST

from comms import push, sms
from comms.models import UrgentNotice
from core import policy

ICONS = [
    ("img/icon-192.png", "192x192", "any"),
    ("img/icon-512.png", "512x512", "any"),
    ("img/icon-maskable-512.png", "512x512", "maskable"),
]
# Static files the service worker keeps for fast, offline-tolerant loading.
# Pages and downloads are never cached: they can hold personal data.
PRECACHE = ["css/site.css", "js/app.js", "js/alerts.js", "img/icons.svg", "img/favicon.svg"]
THEME_COLOR = "#1f5a43"


def _short_name(name: str) -> str:
    if len(name) <= 12:
        return name
    out = ""
    for word in name.split():
        if len(f"{out} {word}".strip()) > 12:
            break
        out = f"{out} {word}".strip()
    return out or name[:12]


@login_required
def manifest(request):
    data = {
        "name": settings.SITE_NAME,
        "short_name": _short_name(settings.SITE_NAME),
        "description": "Private portal for owners and the board.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": static(path), "sizes": sizes, "type": "image/png", "purpose": purpose}
            for path, sizes, purpose in ICONS
        ],
    }
    response = HttpResponse(json.dumps(data), content_type="application/manifest+json")
    response["Cache-Control"] = "private, no-cache"
    return response


@login_required
def service_worker(request):
    precache = [static(p) for p in PRECACHE + [i[0] for i in ICONS]]
    static_prefix = static("")
    config = {
        "cache": "static-" + hashlib.sha256("|".join(precache).encode()).hexdigest()[:12],
        "precache": precache,
        "staticPrefix": static_prefix,
        "siteName": settings.SITE_NAME,
        "icon": static("img/icon-192.png"),
    }
    response = render(
        request,
        "comms/sw.js",
        {"config": json.dumps(config)},
        content_type="application/javascript; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache"
    return response


# --- Push ------------------------------------------------------------------------------


def _json_body(request) -> dict:
    if len(request.body) > 4096:
        return {}
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@login_required
@require_POST
def push_subscribe(request):
    data = _json_body(request)
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    try:
        push.subscribe(
            user=request.user,
            endpoint=str(data.get("endpoint", "")),
            p256dh=str(keys.get("p256dh", "")),
            auth=str(keys.get("auth", "")),
            request=request,
        )
    except push.PushError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def push_unsubscribe(request):
    if request.content_type == "application/json":
        data = _json_body(request)
        removed = push.unsubscribe(
            user=request.user, endpoint=str(data.get("endpoint", "")), request=request
        )
        return JsonResponse({"ok": True, "removed": removed})
    removed = push.unsubscribe(user=request.user, everywhere=True, request=request)
    messages.success(
        request,
        f"Notifications turned off on {removed} device{'s' if removed != 1 else ''}."
        if removed
        else "No devices had notifications on.",
    )
    return redirect("my_profile")


# --- Text messages ---------------------------------------------------------------------


@login_required
@require_POST
def sms_start(request):
    try:
        sms.start_verification(
            user=request.user, number=request.POST.get("number", ""), request=request
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    except sms.SmsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "We texted you a 6-digit code. Enter it below.")
    return redirect("my_profile")


@login_required
@require_POST
def sms_verify(request):
    try:
        number = sms.confirm(user=request.user, code=request.POST.get("code", ""), request=request)
    except sms.SmsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Urgent texts will go to your number {sms.masked(number)}.")
    return redirect("my_profile")


@login_required
@require_POST
def sms_stop(request):
    sms.stop(user=request.user, request=request)
    messages.success(request, "Urgent texts are off. We removed your mobile number.")
    return redirect("my_profile")


class UrgentNoticeForm(forms.Form):
    message = forms.CharField(
        max_length=sms.MESSAGE_MAX,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": sms.MESSAGE_MAX}),
        help_text="Up to 300 characters. No names, addresses, amounts or anything "
        "about one home: put details on the portal.",
    )
    link = forms.ChoiceField(label="Link to", help_text="Owners sign in to open it.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from comms.models import Announcement, Audience

        choices = [(key, label) for key, (label, _) in sms.LINK_CHOICES.items()]
        recent = Announcement.objects.filter(audience=Audience.MEMBERS)[:10]
        choices += [(f"announcement:{a.pk}", f"Announcement: {a.title[:60]}") for a in recent]
        self.fields["link"].choices = choices


@login_required
def urgent_notice(request):
    if not policy.can(request.user, "sms.send_urgent"):
        raise PermissionDenied
    form = UrgentNoticeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            notice = sms.send_urgent_notice(
                actor=request.user,
                message=form.cleaned_data["message"],
                link=form.cleaned_data["link"],
                request=request,
            )
        except sms.SmsError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Sending to {notice.recipients} owner{'s' if notice.recipients != 1 else ''}.",
            )
            return redirect("urgent_notice")
    from community.services import sms_recipients

    return render(
        request,
        "comms/urgent_notice.html",
        {
            "form": form,
            "opted_in": len(sms_recipients()),
            "can_send": sms.can_send(),
            "notices": UrgentNotice.objects.select_related("sent_by")[:20],
            "stop_note": sms.STOP_NOTE,
        },
    )

"""{% app_alerts_panel %}: the "Install the app", push and text message
settings on My profile (Milestone 4). Only the signed-in login's own state."""

from django import template

register = template.Library()


@register.inclusion_tag("comms/partials/app_alerts.html", takes_context=True)
def app_alerts_panel(context):
    from django.conf import settings

    from comms import push, sms
    from community.services import sms_settings
    from core import policy

    request = context["request"]
    user = request.user
    out = {"request": request, "csrf_token": context.get("csrf_token")}
    out["push_on"] = push.available(request)
    if out["push_on"]:
        out["vapid_public_key"] = settings.VAPID_PUBLIC_KEY
        out["push_devices"] = push.device_count(user)
    out["sms_on"] = sms.available(request) and policy.can(user, "sms.opt_in")
    if out["sms_on"]:
        number, opted_in = sms_settings(user)
        out["sms_number"] = sms.masked(number) if opted_in else ""
        out["sms_pending"] = None if opted_in else sms.pending(user)
        out["sms_ready"] = sms.can_send()
        out["stop_note"] = sms.STOP_NOTE
    return out

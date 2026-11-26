"""{% app_alerts_panel %}: the "Install the app" and push settings on My
profile (Milestone 4); {% text_alerts_panel %}: the opt-in urgent texts,
under Phone in "How we reach you" (D40). Only the signed-in login's own state."""

from django import template

register = template.Library()


@register.inclusion_tag("comms/partials/app_alerts.html", takes_context=True)
def app_alerts_panel(context):
    from django.conf import settings

    from comms import push

    request = context["request"]
    user = request.user
    out = {"request": request, "csrf_token": context.get("csrf_token")}
    out["push_on"] = push.available(request)
    out["push_devices"] = push.device_count(user)
    if out["push_on"]:
        out["vapid_public_key"] = settings.VAPID_PUBLIC_KEY
    return out


@register.inclusion_tag("comms/partials/text_alerts.html", takes_context=True)
def text_alerts_panel(context):
    """Off by default and never pre-ticked: ticking "Receive text alerts"
    shows the number field, and the number must be proved with a texted code
    (comms.sms). Nothing about texts shows while the "sms" feature is off,
    except a number still stored, which can always be removed (R2-008)."""
    from comms import sms
    from community.services import sms_settings
    from core import policy

    request = context["request"]
    user = request.user
    out = {"request": request, "csrf_token": context.get("csrf_token")}
    out["sms_on"] = sms.available(request) and policy.can(user, "sms.opt_in")
    number, opted_in = sms_settings(user)
    out["sms_number"] = sms.masked(number) if number else ""
    out["sms_opted_in"] = opted_in
    if out["sms_on"]:
        out["sms_pending"] = None if opted_in else sms.pending(user)
        out["sms_ready"] = sms.can_send()
        out["stop_note"] = sms.STOP_NOTE
    return out

"""Push notifications to the installed app or browser (Milestone 4).

A push carries only a short title and the path of a signed-in page - never
names, addresses, balances or message text. Opening it lands on the portal,
which asks for sign-in as usual. Push is an extra for routine updates sent
through ``comms.notify.send_update``: people on the weekly digest get no
pushes, and legal notices still go by email only.

On only when the board switches the "push" feature on and VAPID keys are set.
Sending happens in the worker (``comms.tasks.send_push``) through
``sender()``; tests replace it with a fake.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from comms import webpush
from comms.models import PushSubscription

logger = logging.getLogger(__name__)

TITLE_MAX = 80
# Review R2-007: a login keeps at most this many devices (the oldest go),
# may save only so many in an hour, and a device is removed after this many
# failed sends in a row.
MAX_DEVICES_PER_LOGIN = 10
MAX_SUBSCRIBES_PER_HOUR = 20
MAX_FAILURES = 5


class PushError(ValueError):
    pass


def configured() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def available(request=None) -> bool:
    from core import features

    return configured() and features.enabled("push", request)


def _audit(action, *, actor, request=None, metadata=None):
    from audit.services import record_event

    record_event(action, actor=actor, request=request, metadata=metadata or {})


def subscribe(*, user, endpoint: str, p256dh: str, auth: str, request=None) -> PushSubscription:
    """Save this device for the signed-in login. An endpoint already saved for
    someone else (a shared computer) moves to the new login."""
    if not available(request):
        raise PushError("Notifications are not switched on.")
    endpoint = (endpoint or "").strip()
    if len(endpoint) > 1000 or not webpush.endpoint_allowed(endpoint):
        raise PushError("This browser's notification service is not supported.")
    try:
        if len(webpush.b64u_decode(p256dh)) != 65 or len(webpush.b64u_decode(auth)) != 16:
            raise ValueError
    except (ValueError, TypeError):
        raise PushError("The browser sent incomplete notification keys.") from None
    if _recent_subscribes(user) >= MAX_SUBSCRIBES_PER_HOUR:
        raise PushError("Too many devices were added in the last hour. Please try again later.")
    with transaction.atomic():
        sub, created = PushSubscription.objects.unscoped(
            "push: one row per endpoint"
        ).update_or_create(
            endpoint=endpoint,
            defaults={
                "user": user,
                "p256dh": p256dh.strip(),
                "auth": auth.strip(),
                "failures": 0,
            },
        )
        _audit("push.subscribed", actor=user, request=request, metadata={"new": created})
        _drop_oldest_devices(user)
    return sub


def _recent_subscribes(user) -> int:
    """Devices this login saved in the last hour (from the audit log, so
    removing and re-adding does not reset the count)."""
    from audit.services import count_recent_events

    return count_recent_events("push.subscribed", actor=user, since=timedelta(hours=1))


def _drop_oldest_devices(user) -> None:
    rows = PushSubscription.objects.unscoped("push: device cap")
    keep = list(
        rows.filter(user=user)
        .order_by("-created_at", "-id")
        .values_list("pk", flat=True)[:MAX_DEVICES_PER_LOGIN]
    )
    rows.filter(user=user).exclude(pk__in=keep).delete()


def unsubscribe(*, user, endpoint: str = "", everywhere: bool = False, request=None) -> int:
    """Remove this device (or every device) of the signed-in login only."""
    mine = PushSubscription.objects.for_user(user)
    if not everywhere:
        mine = mine.filter(endpoint=(endpoint or "").strip())
    removed = len(mine)
    PushSubscription.objects.unscoped("push: own rows").filter(pk__in=[s.pk for s in mine]).delete()
    if removed:
        _audit("push.unsubscribed", actor=user, request=request, metadata={"devices": removed})
    return removed


def device_count(user) -> int:
    return PushSubscription.objects.for_user(user).count()


def _safe_path(link: str) -> str | None:
    """The portal path of ``link`` (a full portal URL or a path), or None."""
    base = settings.PORTAL_BASE_URL
    if link.startswith(base + "/"):
        link = link[len(base) :]
    if not link.startswith("/") or link.startswith("//") or "\\" in link:
        return None
    return link[:500]


def queue_for_users(user_ids, *, title: str, link: str) -> None:
    """After the current transaction commits, push to these logins' devices."""
    if not available():
        return
    path = _safe_path(link)
    user_ids = sorted({int(u) for u in user_ids})
    if not path or not user_ids:
        return
    with_devices = sorted(
        set(
            PushSubscription.objects.unscoped("push: who has devices")
            .filter(user_id__in=user_ids)
            .values_list("user_id", flat=True)
        )
    )
    if not with_devices:
        return
    from comms.tasks import send_push

    title = title[:TITLE_MAX]
    transaction.on_commit(lambda: send_push.defer(user_ids=with_devices, title=title, path=path))


class WebPushSender:
    """Encrypts and posts one message to one subscription."""

    def send(self, sub: PushSubscription, payload: bytes) -> webpush.PushResult:
        body = webpush.encrypt(payload, p256dh=sub.p256dh, auth=sub.auth)
        authorization = webpush.vapid_header(
            sub.endpoint,
            private_key=settings.VAPID_PRIVATE_KEY,
            public_key=settings.VAPID_PUBLIC_KEY,
            subject=settings.VAPID_SUBJECT or settings.PORTAL_BASE_URL,
        )
        return webpush.post(sub.endpoint, body, authorization=authorization)


def sender():
    return WebPushSender()


def deliver(user_ids, *, title: str, path: str) -> dict[str, int]:
    """Send to every device of these logins (worker). Gone devices are removed."""
    counts = {"sent": 0, "failed": 0, "removed": 0}
    if not configured() or _safe_path(path) != path:
        return counts
    try:
        # A broken server key is the server's fault, not the devices': stop
        # here rather than delete every subscription (review R2-005).
        webpush.private_key_from_b64u(settings.VAPID_PRIVATE_KEY)
        webpush.b64u_decode(settings.VAPID_PUBLIC_KEY)
    except (ValueError, TypeError):
        logger.error("push.server_key_invalid")
        return counts
    payload = json.dumps({"title": title[:TITLE_MAX], "url": path}).encode()
    subs = list(
        PushSubscription.objects.unscoped("push: deliver")
        .filter(user_id__in=list(user_ids), user__is_active=True)
        .order_by("pk")
    )
    out = sender()
    for sub in subs:
        try:
            result = out.send(sub, payload)
        except ValueError:  # malformed keys stored by an old browser
            result = webpush.PushResult(ok=False, gone=True)
        rows = PushSubscription.objects.unscoped("push: deliver result").filter(pk=sub.pk)
        if result.ok:
            counts["sent"] += 1
            rows.update(last_sent_at=timezone.now(), failures=0)
        elif result.gone or sub.failures + 1 >= MAX_FAILURES:
            counts["removed"] += 1
            rows.delete()
        else:
            counts["failed"] += 1
            rows.update(failures=F("failures") + 1)
    logger.info("push.delivered", extra=counts)
    return counts

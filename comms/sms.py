"""Opt-in text messages for urgent notices (Milestone 4).

Owners opt in on My profile with a mobile number, proved by a 6-digit code
texted to it (only a keyed hash is stored; ten minutes; a few tries; a few
codes an hour). The board sends an urgent notice - a short text and a link to
a signed-in page - to everyone who opted in. Texts never carry names,
addresses, balances or anything about one lot. Every message ends with the
STOP note; a carrier-level STOP reported by the provider turns the number off.

Providers follow the payments.providers pattern: ``none`` (default, nothing
is sent) and a Twilio-compatible HTTP client built on the standard library.
Tests replace ``provider()`` with a fake; no test touches the network.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from comms.models import SmsVerification, UrgentNotice

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
MAX_CODES_PER_HOUR = 3
MESSAGE_MAX = 300
STOP_NOTE = "Reply STOP to stop these texts."


class SmsError(ValueError):
    pass


class SmsNotConfigured(SmsError):
    pass


class RecipientOptedOut(SmsError):
    """The carrier or provider says this number replied STOP."""


# --- Providers ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    ok: bool
    opted_out: bool = False


class SmsProvider:
    name = "none"
    available = False

    def send(self, to: str, body: str) -> SendResult:
        raise SmsNotConfigured("Text messages are not set up.")


class TwilioProvider(SmsProvider):
    """Twilio's Messages API (or a compatible service at SMS_API_BASE)."""

    name = "twilio"
    OPTED_OUT_CODES = {21610}  # "Attempt to send to unsubscribed recipient"

    def __init__(self):
        self.sid = settings.SMS_ACCOUNT_SID
        self.token = settings.SMS_AUTH_TOKEN
        self.sender = settings.SMS_FROM
        self.base = settings.SMS_API_BASE.rstrip("/")
        self.available = bool(
            self.sid and self.token and self.sender and self.base.startswith("https://")
        )

    def send(self, to: str, body: str) -> SendResult:
        if not self.available:
            raise SmsNotConfigured("Text messages are not set up.")
        url = (
            f"{self.base}/2010-04-01/Accounts/{urllib.parse.quote(self.sid, safe='')}/Messages.json"
        )
        data = urllib.parse.urlencode({"To": to, "From": self.sender, "Body": body}).encode()
        credentials = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        request = urllib.request.Request(  # noqa: S310 - https base checked above
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
                return SendResult(ok=200 <= response.status < 300)
        except urllib.error.HTTPError as exc:
            code = None
            with contextlib.suppress(ValueError, AttributeError):
                code = json.loads(exc.read(4096) or b"{}").get("code")
            logger.warning("sms.send_failed", extra={"status": exc.code, "code": code})
            return SendResult(ok=False, opted_out=code in self.OPTED_OUT_CODES)
        except (urllib.error.URLError, TimeoutError, OSError):
            logger.warning("sms.send_failed", extra={"status": 0})
            return SendResult(ok=False)


def provider() -> SmsProvider:
    if settings.SMS_PROVIDER == "twilio":
        return TwilioProvider()
    return SmsProvider()


def available(request=None) -> bool:
    """The board switched the feature on (the provider may still be unset)."""
    from core import features

    return features.enabled("sms", request)


def can_send() -> bool:
    return provider().available


# --- Numbers -------------------------------------------------------------------------


def normalize_number(raw: str) -> str:
    """US/Canada mobile numbers to +1XXXXXXXXXX; other countries in +E.164."""
    value = (raw or "").strip()
    digits = re.sub(r"\D", "", value)
    if value.startswith("+") and not value.startswith("+1"):
        if 8 <= len(digits) <= 15:
            return "+" + digits
        raise ValidationError("Enter a mobile number, for example 215-555-0123.")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        raise ValidationError("Enter a 10-digit mobile number, for example 215-555-0123.")
    return "+1" + digits


def masked(number: str) -> str:
    return f"ending in {number[-4:]}" if number else ""


def _hash(user_id: int, number: str, code: str) -> str:
    return salted_hmac("comms.sms.verify", f"{user_id}:{number}:{code}").hexdigest()


def _audit(action, *, actor, request=None, metadata=None):
    from audit.services import record_event

    record_event(action, actor=actor, request=request, metadata=metadata or {})


# --- Opt-in --------------------------------------------------------------------------


def _require_owner(user) -> None:
    from community.services import _refuse_read_only
    from core import policy

    _refuse_read_only(user)
    policy.require(user, "sms.opt_in")


def start_verification(*, user, number: str, request=None) -> SmsVerification:
    """Text a 6-digit code to ``number`` for this login."""
    _require_owner(user)
    if not available(request):
        raise SmsError("Text messages are not switched on.")
    number = normalize_number(number)
    out = provider()
    if not out.available:
        raise SmsNotConfigured("Text messages are not set up yet. Please try again later.")
    hour_ago = timezone.now() - timedelta(hours=1)
    recent = SmsVerification.objects.unscoped("sms: rate limit")
    if (
        recent.filter(user=user, created_at__gte=hour_ago).count() >= MAX_CODES_PER_HOUR
        or recent.filter(number=number, created_at__gte=hour_ago).count() >= MAX_CODES_PER_HOUR
    ):
        raise SmsError("Too many codes were sent in the last hour. Please try again later.")
    code = f"{secrets.randbelow(10**6):06d}"
    now = timezone.now()
    with transaction.atomic():
        # Any earlier code for this login stops working.
        SmsVerification.objects.unscoped("sms: supersede").filter(
            user=user, used_at__isnull=True
        ).update(used_at=now)
        verification = SmsVerification.objects.create(
            user=user,
            number=number,
            code_hash=_hash(user.pk, number, code),
            created_at=now,
            expires_at=now + CODE_TTL,
        )
    result = out.send(
        number,
        f"{settings.SITE_NAME}: your code is {code}. It expires in 10 minutes. {STOP_NOTE}",
    )
    if not result.ok:
        raise SmsError("We couldn't text that number. Check it and try again.")
    _audit("sms.code_sent", actor=user, request=request)
    return verification


def pending(user) -> SmsVerification | None:
    return (
        SmsVerification.objects.for_user(user)
        .filter(used_at__isnull=True, expires_at__gt=timezone.now(), attempts__lt=MAX_ATTEMPTS)
        .first()
    )


def confirm(*, user, code: str, request=None) -> str:
    """Check the code; on success the number is saved and texts are on."""
    from community import services as community

    _require_owner(user)
    code = re.sub(r"\D", "", code or "")
    with transaction.atomic():
        verification = (
            SmsVerification.objects.for_user(user)
            .select_for_update()
            .filter(used_at__isnull=True, expires_at__gt=timezone.now())
            .first()
        )
        if verification is None or verification.attempts >= MAX_ATTEMPTS:
            raise SmsError("That code has expired. Ask for a new one.")
        SmsVerification.objects.unscoped("sms: attempt").filter(pk=verification.pk).update(
            attempts=verification.attempts + 1
        )
        if len(code) != 6 or not constant_time_compare(
            verification.code_hash, _hash(user.pk, verification.number, code)
        ):
            ok = False
        else:
            ok = True
            SmsVerification.objects.unscoped("sms: used").filter(pk=verification.pk).update(
                used_at=timezone.now()
            )
            community.set_sms_number(user=user, number=verification.number)
            _audit("sms.opted_in", actor=user, request=request)
    if not ok:
        raise SmsError("That code doesn't match. Check the text and try again.")
    return verification.number


def stop(*, user, request=None) -> None:
    from community import services as community

    if community.stop_sms(user=user):
        _audit("sms.opted_out", actor=user, request=request, metadata={"by": "owner"})


# --- Urgent notices ------------------------------------------------------------------

LINK_CHOICES = {
    "home": ("Home page", "home"),
    "announcements": ("Announcements", "announcements"),
    "calendar": ("Calendar", "calendar"),
}


def link_path(choice: str) -> str:
    if choice.startswith("announcement:"):
        from comms.models import Announcement, Audience

        pk = choice.split(":", 1)[1]
        if (
            not pk.isdigit()
            or not Announcement.objects.filter(pk=int(pk), audience=Audience.MEMBERS).exists()
        ):
            raise SmsError("Choose a page to link to.")
        return reverse("announcement_detail", args=[int(pk)])
    if choice not in LINK_CHOICES:
        raise SmsError("Choose a page to link to.")
    return reverse(LINK_CHOICES[choice][1])


def compose(message: str, path: str) -> str:
    return (
        f"{settings.SITE_NAME} urgent: {message} "
        f"Sign in to read more: {settings.PORTAL_BASE_URL}{path} {STOP_NOTE}"
    )


def check_message(message: str) -> str:
    message = " ".join((message or "").split())
    if not message:
        raise SmsError("Write the message.")
    if len(message) > MESSAGE_MAX:
        raise SmsError(f"Keep the message to {MESSAGE_MAX} characters.")
    # Guard against pasting personal details: no email addresses or amounts.
    if "@" in message or "$" in message:
        raise SmsError("Texts can't include email addresses or amounts. Put details on the portal.")
    return message


def send_urgent_notice(*, actor, message: str, link: str, request=None) -> UrgentNotice:
    """Queue an urgent text to every owner who opted in (board only)."""
    from community.services import sms_recipients
    from core import policy

    policy.require(actor, "sms.send_urgent")
    if not available(request):
        raise SmsError("Text messages are not switched on.")
    if not can_send():
        raise SmsNotConfigured("Text messages are not set up yet (no provider).")
    message = check_message(message)
    path = link_path(link)
    recipients = len(sms_recipients())
    if not recipients:
        raise SmsError("No owners have opted in to text messages yet.")
    with transaction.atomic():
        notice = UrgentNotice.objects.create(
            message=message, link_path=path, sent_by=actor, recipients=recipients
        )
        _audit(
            "sms.urgent_queued",
            actor=actor,
            request=request,
            metadata={"notice_id": notice.pk, "recipients": recipients},
        )
        from comms.tasks import send_urgent_sms

        notice_id = notice.pk
        transaction.on_commit(lambda: send_urgent_sms.defer(notice_id=notice_id))
    return notice


def deliver_urgent_notice(notice_id: int) -> dict[str, int]:
    """Worker: text the notice to everyone opted in now. Counts only."""
    from community.services import sms_recipients, stop_sms

    notice = UrgentNotice.objects.get(pk=notice_id)
    counts = {"sent": 0, "failed": 0, "opted_out": 0}
    if notice.finished_at is not None:
        return counts
    body = compose(notice.message, notice.link_path)
    out = provider()
    for number in sms_recipients():
        try:
            result = out.send(number, body)
        except SmsNotConfigured:
            result = SendResult(ok=False)
        if result.ok:
            counts["sent"] += 1
        else:
            counts["failed"] += 1
            if result.opted_out:
                counts["opted_out"] += stop_sms(number=number)
    UrgentNotice.objects.filter(pk=notice.pk).update(
        sent_count=counts["sent"], failed_count=counts["failed"], finished_at=timezone.now()
    )
    _audit(
        "sms.urgent_delivered",
        actor=None,
        metadata={"notice_id": notice.pk, **counts},
    )
    return counts

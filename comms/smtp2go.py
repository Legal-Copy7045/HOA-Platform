"""SMTP2Go: the email relay's HTTP API, in one place (owner's choice, 0.43.0).

Everything the portal knows about SMTP2Go's API is in this module, so a
change on their side is fixed here only:

* Sending: ``POST /v3/email/send`` with the API key in the
  ``X-Smtp2go-Api-Key`` header and a JSON body (``sender``, ``to``, ``cc``,
  ``bcc``, ``subject``, ``text_body``, ``html_body``, ``custom_headers``,
  ``attachments``). The answer's ``data.email_id`` is SMTP2Go's id of the
  message, kept on the portal's ``OutgoingEmail`` record so delivery
  reports can be matched to it later.
* Delivery and bounces: ``POST /v3/activity/search`` (``start_date``,
  ``end_date``, ``limit``, ``continue_token``), read by the worker every few
  minutes (comms/delivery.py). Pull only: SMTP2Go never calls the portal, so
  there is no webhook and no new anonymous page (D24, D74).
* Key check: ``POST /v3/stats/email_summary``, which reads a usage summary
  and sends nothing (``manage.py check_smtp2go``, set-smtp2go.sh).

Safety, as for the other pull integrations (usps_iv.py, sms.py): https only
to SMTP2Go's own API host for ``SMTP2GO_REGION`` (global, us, eu, au),
redirects refused (urllib would copy the key header to whatever host a 30x
names), Python's certificate verification, a timeout (``SMTP2GO_TIMEOUT``).
Errors are reported by HTTP status and a short code only: request and
response bodies, the key and recipient addresses are never logged.

Tests replace ``transport``; nothing in the test suite reaches the network.
SMTP2Go's public reference is https://developers.smtp2go.com/ (it could not
be read when this was written; the request and answer shapes follow its v3
API as documented, and ``normalize_event`` accepts the spellings of event
names seen in its activity reports).
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger("comms.smtp2go")

PROVIDER = "smtp2go"
REGIONS = {
    "global": "api.smtp2go.com",
    "us": "us-api.smtp2go.com",
    "eu": "eu-api.smtp2go.com",
    "au": "au-api.smtp2go.com",
}
API_PREFIX = "/v3/"
KEY_HEADER = "X-Smtp2go-Api-Key"
KEY_RE = re.compile(r"^api-[A-Za-z0-9]{16,64}$")
EMAIL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_RESPONSE = 4_000_000
MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024  # SMTP2Go's own limit is higher
SEARCH_LIMIT = 500
# Headers the API sets from its own fields (never sent as custom headers).
_OWN_HEADERS = {"from", "to", "cc", "bcc", "subject", "content-type", "mime-version"}

# Short codes -> what a sysadmin does about it (System status, set-up wizard).
FAILURE_WORDS = {
    "not_configured": "SMTP2Go is the email provider but no API key is set: a sysadmin runs "
    "sudo bash infra/poc/set-smtp2go.sh on the server",
    "bad_key": "SMTP2GO_API_KEY does not look like an SMTP2Go API key (api-...): run "
    "set-smtp2go.sh again",
    "bad_region": "SMTP2GO_REGION must be global, us, eu or au",
    "auth": "SMTP2Go refused the API key (wrong, revoked, or without the permission needed): "
    "make a new key in SMTP2Go (Sending → API Keys) and run set-smtp2go.sh again",
    "sender": "SMTP2Go refused the sender address: verify the sender domain in SMTP2Go "
    "(Sending → Verified Senders) or fix DEFAULT_FROM_EMAIL with set-smtp2go.sh",
    "rejected": "SMTP2Go refused the message (see SMTP2Go's activity log for the reason)",
    "too_large": "the message's attachments are too large to send",
    "rate_limited": "SMTP2Go asked the portal to slow down (too many requests); the worker "
    "tries again shortly",
    "server": "SMTP2Go's service answered with an error; the worker tries again shortly",
    "network": "SMTP2Go could not be reached (network or timeout); the worker tries again shortly",
    "protocol": "SMTP2Go answered in a way the portal did not expect",
    "bad_host": "the request was not for SMTP2Go's own API host",
}
TRANSIENT = {"rate_limited", "server", "network"}


class Smtp2goError(Exception):
    """A request that did not work: a short code and the HTTP status (0 = none).
    Never carries the key, a body or an address."""

    def __init__(self, code: str, status: int = 0):
        super().__init__(f"SMTP2Go request failed ({code}, HTTP {status})")
        self.code = code
        self.status = status

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT

    @property
    def words(self) -> str:
        return FAILURE_WORDS.get(self.code, "the request to SMTP2Go failed")


@dataclass(frozen=True)
class Config:
    configured: bool
    host: str
    key: str
    problem: str = ""
    code: str = ""

    def __repr__(self) -> str:  # never the key
        return f"Config(configured={self.configured}, host={self.host!r})"


def selected() -> bool:
    """Whether SMTP2Go is the chosen provider (EMAIL_PROVIDER=smtp2go)."""
    return (getattr(settings, "EMAIL_PROVIDER", "smtp") or "smtp").strip().lower() == PROVIDER


def config() -> Config:
    region = (getattr(settings, "SMTP2GO_REGION", "") or "global").strip().lower()
    host = REGIONS.get(region, "")
    key = (getattr(settings, "SMTP2GO_API_KEY", "") or "").strip()
    if not host:
        return Config(False, "", "", FAILURE_WORDS["bad_region"], "bad_region")
    if not key:
        return Config(False, host, "", FAILURE_WORDS["not_configured"], "not_configured")
    if not KEY_RE.match(key):
        return Config(False, host, "", FAILURE_WORDS["bad_key"], "bad_key")
    return Config(True, host, key)


def provider_label() -> str:
    return "SMTP2Go (HTTP API)" if selected() else "SMTP server"


# --- Transport ----------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the key header must never reach another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _transport(url: str, data: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)  # noqa: S310
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310 - https checked
            return response.status, response.read(MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        body = b""
        with contextlib.suppress(Exception):
            body = exc.read(65536)
        return exc.code, body


# Replaced in tests with a fake SMTP2Go.
transport = _transport


def _allowed(url: str, cfg: Config) -> bool:
    parts = urllib.parse.urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.port is None
        and not parts.username
        and bool(parts.hostname)
        and parts.hostname == cfg.host
        and parts.hostname in REGIONS.values()
        and parts.path.startswith(API_PREFIX)
    )


def post(path: str, payload: dict, *, what: str) -> dict:
    """POST ``payload`` to ``/v3/<path>`` and return the answer's ``data``.
    Raises Smtp2goError with a short code. Logs the status and code only."""
    cfg = config()
    if not cfg.configured:
        raise Smtp2goError(cfg.code or "not_configured")
    url = f"https://{cfg.host}{API_PREFIX}{path}"
    if not _allowed(url, cfg):
        raise Smtp2goError("bad_host")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        KEY_HEADER: cfg.key,
    }
    timeout = float(getattr(settings, "SMTP2GO_TIMEOUT", 15) or 15)
    try:
        status, body = transport(url, json.dumps(payload).encode(), headers, timeout)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        logger.warning(
            "smtp2go.request_failed",
            extra={"what": what, "status": 0, "code": "network", "exc_type": type(exc).__name__},
        )
        raise Smtp2goError("network") from exc
    try:
        answer = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        answer = None
    data = answer.get("data") if isinstance(answer, dict) else None
    data = data if isinstance(data, dict) else {}
    code = ""
    if status in (401, 403):
        code = "auth"
    elif status == 429:
        code = "rate_limited"
    elif status >= 500:
        code = "server"
    elif status >= 400 or "error" in data or "error_code" in data:
        text = f"{data.get('error', '')} {data.get('error_code', '')}".lower()
        if "sender" in text or "from address" in text:
            code = "sender"
        elif "api key" in text or "apikey" in text or "unauthor" in text or "permission" in text:
            code = "auth"
        else:
            code = "rejected"
    elif not isinstance(answer, dict):
        code = "protocol"
    if code:
        logger.warning(
            "smtp2go.request_failed", extra={"what": what, "status": status, "code": code}
        )
        raise Smtp2goError(code, status)
    return data


# --- Sending ------------------------------------------------------------------------------


def _single_line(value) -> str:
    return " ".join(str(value or "").split())


def _attachments(message) -> list[dict]:
    out, total = [], 0
    for item in getattr(message, "attachments", None) or []:
        if isinstance(item, tuple | list):  # (filename, content, mimetype)
            filename, content, mimetype = item
            raw = content.encode() if isinstance(content, str) else bytes(content or b"")
        else:  # a MIMEBase part
            filename = item.get_filename() or "attachment"
            mimetype = item.get_content_type()
            raw = item.get_payload(decode=True) or b""
        total += len(raw)
        if total > MAX_ATTACHMENTS_BYTES:
            raise Smtp2goError("too_large")
        out.append(
            {
                "filename": _single_line(filename)[:200] or "attachment",
                "fileblob": base64.b64encode(raw).decode(),
                "mimetype": mimetype or "application/octet-stream",
            }
        )
    return out


def payload_for(message) -> dict:
    """The /email/send body for one Django EmailMessage."""
    headers = dict(getattr(message, "extra_headers", None) or {})
    lower = {k.lower(): k for k in headers}
    sender = headers.get(lower["from"]) if "from" in lower else ""
    sender = _single_line(sender or message.from_email or settings.DEFAULT_FROM_EMAIL)
    custom = [
        {"header": _single_line(name)[:100], "value": _single_line(value)[:900]}
        for name, value in headers.items()
        if name.lower() not in _OWN_HEADERS
    ]
    if message.reply_to and "reply-to" not in lower:
        custom.append({"header": "Reply-To", "value": _single_line(", ".join(message.reply_to))})
    body = {
        "sender": sender,
        "to": list(message.to),
        "subject": _single_line(message.subject),
    }
    if message.cc:
        body["cc"] = list(message.cc)
    if message.bcc:
        body["bcc"] = list(message.bcc)
    if getattr(message, "content_subtype", "plain") == "html":
        body["html_body"] = message.body or ""
    else:
        body["text_body"] = message.body or ""
    for alt in getattr(message, "alternatives", None) or []:
        content, mimetype = alt[0], alt[1]
        if mimetype == "text/html" and "html_body" not in body:
            body["html_body"] = content
    if custom:
        body["custom_headers"] = custom
    attachments = _attachments(message)
    if attachments:
        body["attachments"] = attachments
    return body


def send(message) -> str:
    """Send one message; returns SMTP2Go's email id ("" if it gave none)."""
    data = post("email/send", payload_for(message), what="send")
    succeeded = data.get("succeeded")
    if isinstance(succeeded, int) and succeeded < 1:
        raise Smtp2goError("rejected", 200)
    email_id = str(data.get("email_id") or "")
    return email_id if EMAIL_ID_RE.match(email_id) else ""


def check_key() -> dict:
    """A harmless call that proves the key works: a usage summary (no email
    is sent). Returns {"ok": True} or raises Smtp2goError."""
    post("stats/email_summary", {}, what="check")
    return {"ok": True}


class Smtp2goBackend(BaseEmailBackend):
    """Django email backend over SMTP2Go's HTTP API (EMAIL_PROVIDER=smtp2go).
    core.email.RedirectingBackend wraps it (tags, POC redirect, failure
    notes). After a send, each message carries ``provider_message_id``."""

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            if not message.recipients():
                continue
            try:
                message.provider_message_id = send(message)
            except Smtp2goError:
                if self.fail_silently:
                    continue
                raise
            message.provider = PROVIDER
            sent += 1
        return sent


# --- Activity (delivery, bounces, complaints, unsubscribes) ----------------------------------


DELIVERED, HARD, SOFT, SPAM, UNSUB = "delivered", "hard", "soft", "spam", "unsub"
_EVENT_NAMES = {
    "delivered": DELIVERED,
    "delivery": DELIVERED,
    "hardbounce": HARD,
    "hardbounced": HARD,
    "softbounce": SOFT,
    "softbounced": SOFT,
    "spam": SPAM,
    "spamcomplaint": SPAM,
    "complaint": SPAM,
    "unsubscribe": UNSUB,
    "unsubscribed": UNSUB,
}
_STATUS_RE = re.compile(r"(?<![0-9.])([245])\.(\d{1,3})\.(\d{1,3})(?![0-9.])")
_SMTP_RE = re.compile(r"(?<![0-9])([245]\d\d)(?![0-9])")


@dataclass(frozen=True)
class Event:
    email_id: str
    kind: str  # delivered / hard / soft / spam / unsub
    recipient: str  # hashed by the caller at once; never stored or logged
    at: datetime.datetime
    status: str  # e.g. "5.1.1" (from the SMTP answer; the answer itself is not kept)

    def __repr__(self) -> str:  # never the address
        return f"Event({self.email_id!r}, {self.kind!r}, {self.at.isoformat()})"


def _when(value, now) -> datetime.datetime:
    if isinstance(value, int | float):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
    if isinstance(value, str):
        parsed = parse_datetime(value.strip().replace(" ", "T", 1))
        if parsed is not None:
            if timezone.is_naive(parsed):
                parsed = parsed.replace(tzinfo=datetime.UTC)
            return parsed
    return now


def normalize_event(raw, *, now=None) -> Event | None:
    """One activity entry -> Event, or None for events the portal ignores
    (opens, clicks, processed) and malformed entries."""
    now = now or timezone.now()
    if not isinstance(raw, dict):
        return None
    name = re.sub(r"[^a-z]", "", str(raw.get("event") or raw.get("type") or "").lower())
    response = " ".join(
        str(raw.get(k) or "") for k in ("smtp_response", "response", "bounce_reason", "reason")
    )[:2000]
    m = _STATUS_RE.search(response)
    status = ".".join(m.groups()) if m else ""
    if not status:
        smtp = _SMTP_RE.search(response)
        status = f"{smtp.group(1)[0]}.0.0" if smtp else ""
    kind = _EVENT_NAMES.get(name)
    if kind is None and name in ("bounce", "bounced"):
        sort = str(raw.get("bounce") or raw.get("bounce_type") or "").lower()
        if sort.startswith("hard"):
            kind = HARD
        elif sort.startswith("soft"):
            kind = SOFT
        else:
            kind = HARD if status.startswith("5") else SOFT
    if kind is None:
        return None
    email_id = str(raw.get("email_id") or raw.get("emailid") or raw.get("id") or "")
    if not EMAIL_ID_RE.match(email_id):
        return None
    recipient = str(raw.get("recipient") or raw.get("to") or raw.get("email") or "")
    recipient = recipient.strip().strip("<>").lower()[:254]
    if kind == HARD and not status:
        status = "5.0.0"
    if kind == SOFT and not status:
        status = "4.0.0"
    return Event(
        email_id, kind, recipient, _when(raw.get("date") or raw.get("timestamp"), now), status[:12]
    )


def _iso(at: datetime.datetime) -> str:
    return at.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_activity(start, end, *, continue_token: str = "", now=None) -> tuple[list[Event], str]:
    """One page of activity between ``start`` and ``end``; returns the events
    the portal uses and the token for the next page ("" when done)."""
    body = {"start_date": _iso(start), "end_date": _iso(end), "limit": SEARCH_LIMIT}
    if continue_token:
        body["continue_token"] = continue_token
    data = post("activity/search", body, what="activity")
    raw = data.get("events")
    if not isinstance(raw, list):
        raw = []
    events = [e for e in (normalize_event(r, now=now) for r in raw[: SEARCH_LIMIT * 2]) if e]
    token = data.get("continue_token") or ""
    token = token if isinstance(token, str) and len(token) <= 2000 and raw else ""
    return events, token

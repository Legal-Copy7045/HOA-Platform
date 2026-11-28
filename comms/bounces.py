"""Bounced email (D74): pull only, from a bounce mailbox over IMAP over TLS.

With SMTP2Go as the relay (EMAIL_PROVIDER=smtp2go, 0.43.0) bounces, spam
complaints and unsubscribes come from SMTP2Go's activity API instead
(comms/delivery.py feeds them into ``record`` below) and no bounce mailbox
is needed; the mailbox stays an optional fallback for plain SMTP.

The project takes no inbound webhooks, so bounces are read, every 20
minutes, from a mailbox the association owns (optional feature
``bounce_tracking``; the login only in the server's .env through
``infra/poc/set-bounce-mailbox.sh``, never through the web app).

How a bounce is tied to what was sent
-------------------------------------
Every outgoing email gets a per-message token (``core.email``):
``<kind><time>.<address hash prefix>.<signature>``, in an ``X-Portal-Ref``
header, inside its Message-ID and, when ``BOUNCE_RETURN_PATH`` is set, in
the envelope sender (VERP: ``bounces+<token>@domain``). The token names the
kind of email (official notice, statement, digest...), when it was sent and
the first 16 characters of the keyed hash of its one recipient; it carries
no address. A bounce counts only when it carries a token whose signature
checks, and it can only mark an address the message was sent to (BUG-002 /
EXP-001: every recipient sees its own token, so a report naming any other
address is a forgery): with one recipient, the reported address must hash
to the token's prefix; with several (the prefix is zeros), the address must
be one of the recipients recorded for that token when it was sent
(``OutgoingEmail.address_hashes``), and a token with no such record counts
for nobody. A report that names no address counts for the token's one
recipient only. So a forged report can at most mark the forger's own address.

Reading a bounce safely
-----------------------
Messages over ``MAX_BYTES`` are skipped, only the first ``MAX_BYTES`` are
fetched (``BODY.PEEK[]<0.N>``), parsing is the standard library's ``email``
package, only delivery reports (RFC 3464 ``message/delivery-status``),
returned headers and the first text part are read, and attachments are
never opened. The failed address is hashed at once and never stored or
logged.

Effects
-------
``EmailBounce`` rows (append-only). A hard bounce marks that address as
bouncing (person page for the board, My profile for the person) until the
board marks it fixed or the address changes. A hard bounce of an official
notice (D44) puts "Send a paper copy to <home>" on the board's My items,
linking to Paper mail, until a mailing for that home is prepared or the
board says it was sent another way: legal notices must not silently fail.
"""

from __future__ import annotations

import contextlib
import datetime
import email
import email.policy
import hashlib
import hmac
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from comms.models import BLOCKING_BOUNCES
from core.scoping import system_access

logger = logging.getLogger(__name__)

FEATURE = "bounce_tracking"
MAX_BYTES = 256 * 1024  # never fetch or parse more of one message
TEXT_BYTES = 64 * 1024  # of a text part
MAX_PARTS = 40
BATCH = 200  # messages per check
TIMEOUT_SECONDS = 30
BURST = 5  # hard bounces in one check that go on the incident timeline
TOKEN_DAYS = 45  # a token older than this no longer counts
STALE_HOURS = 2  # no successful read for this long: the health check fails
NOTICE_DAYS = 60  # how long a bounced official notice stays on My items

KIND_CODES = {
    "notice": "n",
    "statement": "s",
    "digest": "d",
    "board": "b",
    "account": "a",
    "other": "o",
}
CODE_KINDS = {v: k for k, v in KIND_CODES.items()}
NO_ADDRESS = "0" * 16
TOKEN_RE = re.compile(
    r"(?<![0-9a-z])([nsdbao])([0-9a-z]{5,9})\.([0-9a-f]{16})\.([0-9a-f]{16})(?![0-9a-z])"
)
STATUS_RE = re.compile(r"(?<![0-9.])([245])\.(\d{1,3})\.(\d{1,3})(?![0-9.])")
SMTP_RE = re.compile(r"(?<![0-9])(55[0-4]|45[0-2])[ -]")
ADDRESS_RE = re.compile(r"[^\s<>;,\"']+@[^\s<>;,\"']+")
FAILURE_SUBJECTS = (
    "undeliver",
    "delivery status notification (failure)",
    "mail delivery failed",
    "returned mail",
    "failure notice",
    "delivery failure",
    "could not be delivered",
)

FAILURE_WORDS = {
    "auth": "the mailbox refused the user name or password",
    "connect": "the mail server could not be reached",
    "tls": "the mail server's certificate did not check out",
    "protocol": "the mail server answered in a way the portal did not expect",
    "folder": "the folder to read was not found",
}


# --- Configuration -------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    folder: str
    configured: bool
    problem: str = ""


_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})+$")


def config() -> Config:
    from core import credentials

    host = (credentials.get("BOUNCE_IMAP_HOST") or "").strip().lower()
    user = credentials.get("BOUNCE_IMAP_USER") or ""
    password = credentials.get("BOUNCE_IMAP_PASSWORD") or ""
    folder = (credentials.get("BOUNCE_IMAP_FOLDER") or "INBOX").strip()
    try:
        port = int(credentials.get("BOUNCE_IMAP_PORT") or 993)
    except (TypeError, ValueError):
        port = 0
    if not host:
        return Config("", 993, "", "", folder, False, "BOUNCE_IMAP_HOST is not set.")
    if not _HOST_RE.match(host):
        return Config(host, port, "", "", folder, False, "BOUNCE_IMAP_HOST is not a host name.")
    if not 1 <= port <= 65535:
        return Config(host, port, "", "", folder, False, "BOUNCE_IMAP_PORT is not a port.")
    # The password is in the worker only (EXP-003); elsewhere the flag says one is set.
    if not (user and (password or getattr(settings, "BOUNCE_IMAP_PASSWORD_SET", False))):
        problem = "BOUNCE_IMAP_USER and BOUNCE_IMAP_PASSWORD are not set."
        return Config(host, port, "", "", folder, False, problem)
    return Config(host, port, user, password, folder, True)


def feature_on(request=None) -> bool:
    from core import features

    return features.enabled(FEATURE, request)


def active() -> bool:
    return feature_on() and config().configured


# --- Hashes and tokens ---------------------------------------------------------------------


def _hash_key() -> bytes:
    base = settings.AUDIT_HASH_KEY or settings.SECRET_KEY
    return (base + "|email-bounce-address").encode()


def address_hash(address: str) -> str:
    """Keyed hash of an address (lower-case, trimmed): how bounces name it."""
    value = (address or "").strip().lower().encode()
    return hmac.new(_hash_key(), value, hashlib.sha256).hexdigest()


def _b36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, r = divmod(number, 36)
        out = digits[r] + out
        if not number:
            return out


def _sign(payload: str) -> str:
    return salted_hmac("comms.bounces.ref", payload, algorithm="sha256").hexdigest()[:16]


def make_ref(kind: str, recipients, *, now=None) -> str:
    """The token for one outgoing message. With one recipient it carries the
    first 16 characters of that address's hash; with several, zeros (the
    bounce report names the failed one)."""
    code = KIND_CODES.get(kind, "o")
    stamp = _b36(int((now or timezone.now()).timestamp()))
    recipients = [r for r in (recipients or []) if r]
    addr = address_hash(recipients[0])[:16] if len(recipients) == 1 else NO_ADDRESS
    payload = f"{code}{stamp}.{addr}"
    return f"{payload}.{_sign(payload)}"


@dataclass(frozen=True)
class Ref:
    kind: str
    sent_at: datetime.datetime
    addr16: str
    token: str = ""


def read_ref(text: str, *, now=None) -> Ref | None:
    """The first token in ``text`` whose signature checks and that is not
    too old (or from the future)."""
    now = now or timezone.now()
    for m in TOKEN_RE.finditer((text or "").lower()):
        code, stamp, addr, sig = m.groups()
        if not hmac.compare_digest(_sign(f"{code}{stamp}.{addr}"), sig):
            continue
        try:
            sent = datetime.datetime.fromtimestamp(int(stamp, 36), tz=datetime.UTC)
        except (ValueError, OverflowError, OSError):
            continue
        if sent > now + datetime.timedelta(days=1):
            continue
        if sent < now - datetime.timedelta(days=TOKEN_DAYS):
            continue
        return Ref(CODE_KINDS[code], sent, addr, f"{code}{stamp}.{addr}.{sig}")
    return None


def return_path(ref: str) -> str:
    """VERP envelope sender for a token when BOUNCE_RETURN_PATH is set
    (``bounces@example.org`` -> ``bounces+<token>@example.org``), else ''."""
    base = (getattr(settings, "BOUNCE_RETURN_PATH", "") or "").strip()
    local, at, domain = base.partition("@")
    if not (local and at and domain) or "+" in local:
        return ""
    return f"{local}+{ref}@{domain}"


# --- Parsing a bounce ----------------------------------------------------------------------


@dataclass
class Parsed:
    address_hash: str  # full hash of the failed address, or "" when only the token names it
    addr16: str
    kind: str  # hard / soft
    status: str
    mail_kind: str
    sent_at: datetime.datetime
    dedupe: str
    # A several-recipient token: record() accepts the address only if it was
    # one of that message's recipients (BUG-002).
    ref: str = ""
    check_recipients: bool = False


def _text(value) -> str:
    if value is None:
        return ""
    with contextlib.suppress(Exception):
        return str(email.header.make_header(email.header.decode_header(str(value))))
    return str(value)


def _headers_text(msg) -> str:
    out = []
    with contextlib.suppress(Exception):
        for name, value in msg.items():
            out.append(f"{name}: {_text(value)}")
    return "\n".join(out)[:TEXT_BYTES]


def _payload_text(part) -> str:
    with contextlib.suppress(Exception):
        raw = part.get_payload(decode=True) or b""
        return raw[:TEXT_BYTES].decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _status_blocks(part) -> list:
    """The per-recipient blocks of a message/delivery-status part."""
    payload = part.get_payload()
    if isinstance(payload, list):
        return [p for p in payload if hasattr(p, "get")]
    if isinstance(payload, str):  # some servers send it as plain text
        blocks = []
        for chunk in re.split(r"\r?\n\r?\n", payload[:TEXT_BYTES]):
            with contextlib.suppress(Exception):
                blocks.append(email.message_from_string(chunk.strip() + "\n"))
        return blocks
    return []


def _address_of(field: str) -> str:
    """'rfc822; someone@example.org' -> the address (lower-case)."""
    value = (field or "").split(";", 1)[-1].strip().strip("<>")
    m = ADDRESS_RE.search(value)
    return m.group(0).lower() if m else ""


def _walk(msg):
    """Parts to read, without descending into returned messages' bodies and
    without opening anything that is not text or a report."""
    stack, seen = [msg], 0
    while stack and seen < MAX_PARTS:
        part = stack.pop(0)
        seen += 1
        yield part
        ctype = part.get_content_type()
        if part.is_multipart() and ctype != "message/rfc822" and ctype.startswith("multipart/"):
            with contextlib.suppress(Exception):
                stack.extend(part.get_payload())


def parse(raw: bytes, *, now=None) -> list[Parsed]:
    """The failed deliveries a bounce reports, or [] (not a bounce, not one
    of ours, or an out-of-office reply). Never raises on bad input."""
    try:
        return _parse(raw[:MAX_BYTES], now=now)
    except Exception as exc:  # hostile input: the type only
        logger.warning("bounces.unreadable", extra={"exc_type": type(exc).__name__})
        return []


def _parse(raw: bytes, *, now=None) -> list[Parsed]:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    texts = [
        " ".join(
            _text(msg.get(h, ""))
            for h in ("To", "Delivered-To", "X-Original-To", "Envelope-To", "X-Envelope-To")
        )
    ]
    reports: list[tuple[str, str, str]] = []  # (address, action, status)
    body_text = ""
    for part in _walk(msg):
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            for block in _status_blocks(part):
                address = _address_of(_text(block.get("Final-Recipient") or "")) or _address_of(
                    _text(block.get("Original-Recipient") or "")
                )
                action = _text(block.get("Action") or "").strip().lower()
                status = _text(block.get("Status") or "").strip()
                if address or action or status:
                    reports.append((address, action, status))
        elif ctype == "text/rfc822-headers":
            texts.append(_payload_text(part))
        elif ctype == "message/rfc822":
            with contextlib.suppress(Exception):
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                if hasattr(inner, "items"):
                    texts.append(_headers_text(inner))
        elif ctype == "text/plain" and not part.get_filename() and len(body_text) < TEXT_BYTES:
            body_text += _payload_text(part)
    texts.append(body_text)
    ref = read_ref("\n".join(texts), now=now)
    if ref is None:
        return []

    subject = _text(msg.get("Subject", "")).lower()
    failed: list[tuple[str, str]] = []  # (address or "", status)
    if reports:
        for address, action, status in reports:
            if action in ("delivered", "relayed", "expanded"):
                continue
            m = STATUS_RE.search(status)
            code = ".".join(m.groups()) if m else ""
            if action == "delayed" and not code:
                code = "4.0.0"
            if code.startswith("2"):
                continue
            if not code and action != "failed":
                continue
            failed.append((address, code or "5.0.0"))
    else:
        m = STATUS_RE.search(body_text)
        code = ".".join(m.groups()) if m and m.group(1) in "45" else ""
        if not code:
            smtp = SMTP_RE.search(body_text)
            if smtp:
                code = f"{smtp.group(1)[0]}.0.0"
        if not code and not any(s in subject for s in FAILURE_SUBJECTS):
            return []  # an auto-reply or ordinary mail, not a bounce
        x_failed = _text(msg.get("X-Failed-Recipients", ""))
        addresses = [a.lower() for a in ADDRESS_RE.findall(x_failed)][:10]
        status = code or "4.0.0"  # a failure we cannot classify counts as soft
        failed = [(a, status) for a in addresses] or [("", status)]

    ident = _text(msg.get("Message-ID", "")).strip() or hashlib.sha256(raw[:4096]).hexdigest()
    out, seen = [], set()
    for address, status in failed:
        full = address_hash(address) if address else ""
        if not full and ref.addr16 == NO_ADDRESS:
            continue  # nothing names whose address failed
        if full and ref.addr16 != NO_ADDRESS and not hmac.compare_digest(full[:16], ref.addr16):
            continue  # not the address this message went to: a forged report (BUG-002)
        key = full or ref.addr16
        if key in seen:
            continue
        seen.add(key)
        dedupe = hashlib.sha256(f"{ident}|{key}".encode()).hexdigest()
        out.append(
            Parsed(
                address_hash=full,
                addr16=full[:16] if full else ref.addr16,
                kind="hard" if status.startswith("5") else "soft",
                status=status[:12],
                mail_kind=ref.kind,
                sent_at=ref.sent_at,
                dedupe=dedupe,
                ref=ref.token,
                check_recipients=ref.addr16 == NO_ADDRESS,
            )
        )
    return out


# --- Matching and recording ----------------------------------------------------------------


def _known_addresses() -> dict[str, tuple[int, str]]:
    """address hash -> (person id, full hash) for every address on file:
    logins, people's own email and their extra email addresses."""
    from community.models import ContactMethod, ContactMethodKind, Person

    out: dict[str, tuple[int, str]] = {}
    with system_access("bounce matching"):
        for pk, own, login in Person.objects.values_list("pk", "email", "user__email"):
            for address in (own, login):
                if address:
                    h = address_hash(address)
                    out.setdefault(h, (pk, h))
        for pk, value in ContactMethod.objects.filter(kind=ContactMethodKind.EMAIL).values_list(
            "person_id", "value"
        ):
            if value:
                h = address_hash(value)
                out.setdefault(h, (pk, h))
    return out


def _match(p: Parsed, known: dict) -> tuple[int | None, str]:
    if p.address_hash:
        found = known.get(p.address_hash)
        return (found[0] if found else None), p.address_hash
    for h, (pk, full) in known.items():
        if h.startswith(p.addr16):
            return pk, full
    return None, p.addr16


def _was_recipient(ref: str, full_hash: str) -> bool:
    """Whether the message sent with this token went to this address (the
    recipients' hashes were recorded at send time, comms.delivery)."""
    from comms.models import OutgoingEmail

    if not (ref and full_hash):
        return False
    with system_access("bounce recipients"):
        hashes = OutgoingEmail.objects.filter(ref=ref).values_list("address_hashes", flat=True)
        return any(full_hash in (h or "").split() for h in hashes)


def record(parsed: list[Parsed], *, known=None, now=None) -> tuple[int, int]:
    """Store each failed delivery once. Returns (recorded, of them hard)."""
    from comms.models import EmailBounce

    known = _known_addresses() if known is None else known
    recorded = hard = 0
    for p in parsed:
        if p.check_recipients and not _was_recipient(p.ref, p.address_hash):
            logger.warning("bounces.not_a_recipient")  # a forged report (BUG-002)
            continue
        person_id, full = _match(p, known)
        try:
            with transaction.atomic():
                EmailBounce.objects.create(
                    person_id=person_id,
                    address_hash=full,
                    kind=p.kind,
                    mail_kind=p.mail_kind,
                    status=p.status,
                    sent_at=p.sent_at,
                    received_at=now or timezone.now(),
                    dedupe=p.dedupe,
                )
        except IntegrityError:  # read before (the same bounce again)
            continue
        recorded += 1
        hard += p.kind == "hard"
    return recorded, hard


# --- The mailbox ---------------------------------------------------------------------------


class MailboxError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def tls_context() -> ssl.SSLContext:
    """Certificates and the host name are verified (the default context),
    TLS 1.2 or newer."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")


class Mailbox:
    """A short IMAP session: sign in, select the folder, read unseen
    messages, mark them seen, sign out. ``factory`` is imaplib.IMAP4_SSL
    (tests pass a fake)."""

    def __init__(self, cfg: Config, factory=None):
        self.cfg = cfg
        self.factory = factory or imaplib.IMAP4_SSL
        self.conn = None

    def __enter__(self):
        try:
            self.conn = self.factory(
                self.cfg.host, self.cfg.port, ssl_context=tls_context(), timeout=TIMEOUT_SECONDS
            )
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise MailboxError("tls") from exc
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxError("connect") from exc
        try:
            self.conn.login(self.cfg.user, self.cfg.password)
        except imaplib.IMAP4.error as exc:
            self._close()
            raise MailboxError("auth") from exc
        except OSError as exc:
            self._close()
            raise MailboxError("connect") from exc
        try:
            typ, _ = self.conn.select(self._quoted(self.cfg.folder))
        except (imaplib.IMAP4.error, OSError) as exc:
            self._close()
            raise MailboxError("protocol") from exc
        if typ != "OK":
            self._close()
            raise MailboxError("folder")
        return self

    @staticmethod
    def _quoted(folder: str) -> str:
        clean = "".join(ch for ch in folder if ch.isprintable() and ch not in '"\\')[:100]
        return f'"{clean or "INBOX"}"'

    def _close(self):
        with contextlib.suppress(Exception):
            self.conn.logout()

    def __exit__(self, *exc):
        self._close()
        return False

    def _ok(self, typ):
        if typ != "OK":
            raise MailboxError("protocol")

    def unseen(self, limit: int = BATCH) -> list[bytes]:
        try:
            typ, data = self.conn.uid("SEARCH", None, "UNSEEN")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailboxError("protocol") from exc
        self._ok(typ)
        uids = (data[0] or b"").split() if data else []
        return [u for u in uids if u.isdigit()][:limit]

    def fetch(self, uid: bytes) -> bytes | None:
        """The message's first MAX_BYTES, or None when it is larger than that
        (skipped: no bounce report is that big)."""
        try:
            typ, data = self.conn.uid("FETCH", uid, "(RFC822.SIZE)")
            self._ok(typ)
            head = b" ".join(d if isinstance(d, bytes) else b"" for d in data or [])
            m = _SIZE_RE.search(head)
            if m is None or int(m.group(1)) > MAX_BYTES:
                return None
            typ, data = self.conn.uid("FETCH", uid, f"(BODY.PEEK[]<0.{MAX_BYTES}>)")
            self._ok(typ)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailboxError("protocol") from exc
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return item[1][:MAX_BYTES]
        return None

    def mark_seen(self, uid: bytes) -> None:
        try:
            typ, _ = self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailboxError("protocol") from exc
        self._ok(typ)


def check_login(factory=None) -> None:
    """Sign in and select the folder only (check_bounce_mailbox)."""
    cfg = config()
    if not cfg.configured:
        raise MailboxError("not_configured")
    with Mailbox(cfg, factory):
        pass


def poll(*, factory=None, now=None) -> dict:
    """Read unseen messages once (the periodic task). Returns counts."""
    from comms.models import BouncePollState

    cfg = config()
    if not cfg.configured:
        return {"state": "not_configured"}
    now = now or timezone.now()
    state = BouncePollState.get()
    state.last_attempt_at = now
    read = recorded = hard = unmatched = skipped = 0
    try:
        with Mailbox(cfg, factory) as box:
            known = None
            for uid in box.unseen():
                raw = box.fetch(uid)
                read += 1
                if raw is None:
                    skipped += 1
                else:
                    parsed = parse(raw, now=now)
                    if not parsed:
                        unmatched += 1
                    else:
                        known = _known_addresses() if known is None else known
                        r, h = record(parsed, known=known, now=now)
                        recorded += r
                        hard += h
                box.mark_seen(uid)
    except MailboxError as exc:
        state.last_failure_at = now
        state.last_failure = exc.code[:20]
        state.save()
        logger.warning("bounces.poll_failed", extra={"code": exc.code})
        return {"state": "failed", "code": exc.code, "read": read}
    state.last_success_at = now
    state.last_failure = ""
    state.last_read, state.last_recorded, state.last_unmatched = read, recorded, unmatched
    state.save()
    if hard >= BURST:
        from core import ops_timeline

        ops_timeline.record(
            "email",
            f"{hard} hard bounces read in one check of the bounce mailbox (addresses that do "
            "not accept mail): see People and My items",
            key="bounces",
            state="problem",
            count=hard,
        )
    if read:
        from audit.models import Channel
        from audit.services import record_event

        record_event(
            "comms.bounces_read",
            actor=None,
            channel=Channel.WORKER,
            metadata={
                "read": read,
                "recorded": recorded,
                "hard": hard,
                "unmatched": unmatched,
                "skipped": skipped,
            },
        )
    return {
        "state": "ok",
        "read": read,
        "recorded": recorded,
        "hard": hard,
        "unmatched": unmatched,
        "skipped": skipped,
    }


# --- Status: health, set-up, people --------------------------------------------------------


def _ago(at, now) -> str:
    minutes = int((now - at).total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def _smtp2go() -> bool:
    from comms import smtp2go

    return smtp2go.selected()


def health():
    """core.health "Bounce mailbox": hidden while not set up and off, and
    while SMTP2Go reports the bounces."""
    from comms.models import BouncePollState, EmailBounce

    cfg = config()
    on = feature_on()
    if not cfg.configured:
        if _smtp2go():
            return None  # SMTP2Go reports bounces: no mailbox needed
        if on:
            return False, (
                "Bounce tracking is switched on but no bounce mailbox is set up: a sysadmin "
                "runs sudo bash infra/poc/set-bounce-mailbox.sh on the server (or switches "
                "email to SMTP2Go with set-smtp2go.sh, which needs no mailbox)"
            )
        return None
    if not on:
        return True, "Mailbox set up; bounce tracking is switched off (Settings → Features (board))"
    now = timezone.now()
    state = BouncePollState.objects.filter(pk=1).first()
    if state is None or state.last_attempt_at is None:
        return True, "Set up; the first check runs within 20 minutes"
    failing = state.last_failure and (
        state.last_success_at is None or state.last_failure_at >= state.last_success_at
    )
    stale = state.last_success_at is None or state.last_success_at < now - datetime.timedelta(
        hours=STALE_HOURS
    )
    if failing and stale:
        words = FAILURE_WORDS.get(state.last_failure, "reading the mailbox failed")
        since = state.last_success_at
        return False, (
            f"Bounced email is not being read: {words} (last read "
            f"{_ago(since, now) if since else 'never'}). On the server run "
            "manage.py check_bounce_mailbox; to change the login, run set-bounce-mailbox.sh again"
        )
    week = now - datetime.timedelta(days=7)
    with system_access("bounce counts"):
        hard = EmailBounce.objects.filter(kind="hard", received_at__gte=week).count()
    return True, (
        f"Last read {_ago(state.last_success_at, now)}; {hard} hard bounce"
        f"{'' if hard == 1 else 's'} in the last 7 days"
    )


def setup_status() -> dict:
    """For the set-up wizard's server step."""
    cfg = config()
    if not cfg.configured and _smtp2go():
        return {
            "state": "not_needed",
            "text": "Not needed: SMTP2Go reports bounces, spam complaints and unsubscribes "
            "to the portal (Email delivery reports).",
        }
    if not cfg.configured:
        return {
            "state": "not_set_up",
            "text": "Not set up: a sysadmin runs sudo bash infra/poc/set-bounce-mailbox.sh on "
            "the server to enter the bounce mailbox's login. Email works without it; bounced "
            "official notices are then not noticed.",
        }
    try:
        result = health()
    except Exception as exc:  # shown like a failed check
        return {"state": "failing", "text": f"Check failed ({type(exc).__name__})"}
    ok, detail = result if result else (True, "Set up")
    return {"state": "set_up" if ok else "failing", "text": f"Set up ({cfg.host}). {detail}."}


def _person_addresses(person) -> list[str]:
    from community.models import ContactMethod, ContactMethodKind

    addresses = []
    with system_access("person addresses for bounces"):
        login = person.user.email if person.user_id else ""
        for a in [login, person.email]:
            if a and a.lower() not in [x.lower() for x in addresses]:
                addresses.append(a)
        for value in ContactMethod.objects.filter(
            person=person, kind=ContactMethodKind.EMAIL
        ).values_list("value", flat=True):
            if value and value.lower() not in [x.lower() for x in addresses]:
                addresses.append(value)
    return addresses


def bouncing(person) -> list[dict]:
    """The person's addresses with a hard bounce (or, from SMTP2Go, a spam
    complaint or unsubscribe) not cleared since:
    [{"address", "since" (first), "last", "mail_kind"}]. For the board's
    person page and the person's own My profile only."""
    from comms.models import EmailBounce, EmailBounceClear

    if person is None:
        return []
    out = []
    with system_access("bouncing addresses"):
        for address in _person_addresses(person):
            h = address_hash(address)
            cleared = (
                EmailBounceClear.objects.filter(person=person, address_hash=h)
                .order_by("-cleared_at")
                .values_list("cleared_at", flat=True)
                .first()
            )
            rows = EmailBounce.objects.filter(address_hash=h, kind__in=BLOCKING_BOUNCES)
            if cleared:
                rows = rows.filter(received_at__gt=cleared)
            rows = list(rows.order_by("received_at"))
            if rows:
                out.append(
                    {
                        "address": address,
                        "since": rows[0].received_at,
                        "last": rows[-1].received_at,
                        "mail_kind": rows[-1].get_mail_kind_display(),
                        "why": rows[-1].get_kind_display(),
                        "count": len(rows),
                    }
                )
    return out


def clear(person, *, actor, request=None) -> int:
    """The board marks the person's bouncing addresses fixed (person.manage).
    Audited with ids and counts only. Returns how many were cleared."""
    from audit.services import record_event
    from comms.models import EmailBounceClear
    from core import policy

    policy.require(actor, "person.manage")
    rows = bouncing(person)
    with transaction.atomic():
        for row in rows:
            EmailBounceClear.objects.create(
                person=person, address_hash=address_hash(row["address"]), cleared_by=actor
            )
        record_event(
            "comms.bounce_cleared",
            actor=actor,
            request=request,
            target=person,
            metadata={"person_id": person.pk, "addresses": len(rows)},
        )
    return len(rows)


# --- Official notices that bounced: paper copies -------------------------------------------


@dataclass
class PaperCopy:
    lot: object
    bounces: list
    first: datetime.datetime


def paper_copies_needed(now=None) -> list[PaperCopy]:
    """Homes whose owner's official notice bounced (hard) in the last
    NOTICE_DAYS, with no paper mailing for that home prepared since and no
    "sent another way" from the board. One entry per home, oldest first."""
    from comms.models import BounceFollowUp, EmailBounce, Mailing
    from community.models import Lot, Ownership

    now = now or timezone.now()
    since = now - datetime.timedelta(days=NOTICE_DAYS)
    by_lot: dict[int, list] = {}
    with system_access("bounced official notices"):
        rows = list(
            EmailBounce.objects.filter(
                kind__in=BLOCKING_BOUNCES,
                mail_kind="notice",
                person__isnull=False,
                received_at__gte=since,
            ).order_by("received_at")
        )
        if not rows:
            return []
        done = set(BounceFollowUp.objects.values_list("bounce_id", "lot_id"))
        owners = {}
        for person_id, lot_id in (
            Ownership.objects.active()
            .filter(person_id__in={r.person_id for r in rows})
            .values_list("person_id", "lot_id")
        ):
            owners.setdefault(person_id, set()).add(lot_id)
        mailings = list(
            Mailing.objects.filter(created_at__gte=since).values_list("created_at", "groups")
        )
        for r in rows:
            for lot_id in owners.get(r.person_id, ()):
                if (r.pk, lot_id) in done:
                    continue
                if any(
                    at > r.received_at
                    and any(str(g.get("lot")) == str(lot_id) for g in (groups or []))
                    for at, groups in mailings
                ):
                    continue
                by_lot.setdefault(lot_id, []).append(r)
        lots = {lot.pk: lot for lot in Lot.objects.filter(pk__in=by_lot)}
    out = [PaperCopy(lots[k], v, v[0].received_at) for k, v in by_lot.items() if k in lots]
    out.sort(key=lambda c: c.first)
    return out


def paper_url(lot) -> str:
    """Paper mail's new letter for this one home, whatever its email choice."""
    from django.urls import reverse

    return reverse("paper_mail_new") + f"?home={lot.pk}&everyone=1"


def paper_items() -> list:
    """Board and manager My items (``mail.print``)."""
    from django.urls import reverse

    from core.dates import short_date
    from core.my_items import Item

    if not feature_on():
        return []
    out = []
    for copy in paper_copies_needed():
        sent = copy.bounces[0].sent_at or copy.first
        out.append(
            Item(
                "Paper mail",
                f"Send a paper copy to {copy.lot.label}",
                f"An official notice emailed {short_date(timezone.localdate(sent))} bounced: "
                "the owner's address does not accept mail",
                reverse("paper_mail") + "#bounced",
                "danger",
                timezone.localdate(copy.first),
                action_label="Prepare the letter",
                action_url=paper_url(copy.lot),
            )
        )
    return out


def mark_paper_sent(lot, *, actor, request=None) -> int:
    """The board or manager sent the paper copy another way (``mail.print``).
    Closes every open bounced notice of that home. Audited, ids only."""
    from audit.services import record_event
    from comms.models import BounceFollowUp
    from core import policy

    policy.require(actor, "mail.print")
    open_ = next((c for c in paper_copies_needed() if c.lot.pk == lot.pk), None)
    if open_ is None:
        return 0
    with transaction.atomic():
        for b in open_.bounces:
            with contextlib.suppress(IntegrityError), transaction.atomic():
                BounceFollowUp.objects.create(bounce=b, lot=lot, done_by=actor)
        record_event(
            "comms.bounce_paper_sent",
            actor=actor,
            request=request,
            target=lot,
            metadata={"lot_id": lot.pk, "bounces": len(open_.bounces)},
        )
    return len(open_.bounces)

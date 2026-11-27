"""Keys and connections (decision D93): third-party service keys and settings
entered in the browser, stored encrypted, read through one accessor.

* ``get(name)`` is the only way the portal reads these settings: the value a
  sysadmin saved on System status -> Keys and connections when there is one,
  else the server's environment (``.env`` through Django settings), so an
  install set up with the server scripts keeps working and they stay a
  fallback. Web and worker both read the database, so which container was
  given which environment variable (EXP-003) no longer matters for them.
* Storage is append-only (``CredentialChange`` / ``CredentialValue`` /
  ``CredentialDecision``, model + trigger + grants). Each value is sealed with
  AES-256-GCM under a key derived with HKDF-SHA256 (its own salt and label)
  from ``CREDENTIALS_KEK``, a key-encryption key that lives only in the
  server's ``.env`` (created there automatically) and never in the database,
  the backups or DJANGO_SECRET_KEY. The associated data binds a value to its
  name and change, so a sealed value cannot be moved to another setting.
* Write-only: no page, log, audit entry, email, export or error report ever
  carries a value. The page shows Set / Not set, the last four characters of a
  secret (after a fresh passkey or authenticator check), who saved it and when,
  and the last test.
* Fails closed: a value that cannot be opened (the KEK is missing or is not
  the one it was sealed with) reads as empty, never as the older ``.env``
  value, and the page says why.
* Changes take effect without a restart: each process keeps what it read for
  ``CREDENTIALS_CACHE_SECONDS`` (15 s) and forgets it at once on its own saves.
* Stripe and Teller changes wait for a board member other than the person who
  entered them (48 hours), and their provider account is pinned: a key for a
  different account is refused, and moving the pin needs two board members
  plus a 72-hour wait, with the board told at every step.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

logger = logging.getLogger(__name__)

FORMAT = b"SC1\x00"
NONCE_BYTES = 12
MIN_KEK_LENGTH = 32
_SALT = b"oak-creek-portal service credentials"
_INFO = b"oak-creek-portal service credentials aes-256-gcm v1"

APPROVAL_HOURS = 48  # a Stripe or Teller change waits this long for a board member
PIN_CHANGE_APPROVALS = 2
PIN_CHANGE_WAIT_HOURS = 72
PIN_CHANGE_EXPIRY_DAYS = 7
SAVES_PER_HOUR = 10  # per sysadmin (tests count separately, TESTS_PER_HOUR)
TESTS_PER_HOUR = 30

TEXT, SECRET, CHOICE, BOOL, INT, FILE, GENERATED, LIST = (
    "text",
    "secret",
    "choice",
    "bool",
    "int",
    "file",
    "generated",
    "list",
)
SECRET_KINDS = frozenset({SECRET, FILE, GENERATED, LIST})


class CredentialError(Exception):
    """A save, test or decision was refused; the message is safe to show."""


@dataclass(frozen=True)
class Field:
    name: str  # the environment variable it replaces
    label: str
    kind: str = TEXT
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    required: bool = True
    max_length: int = 200

    @property
    def secret(self) -> bool:
        return self.kind in SECRET_KINDS


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    summary: str
    fields: tuple[Field, ...]
    # Set alongside every save (e.g. EMAIL_PROVIDER=smtp2go).
    implied: dict = field(default_factory=dict)
    approval: bool = False  # Stripe, Teller: a second person approves (D93)
    pinned: str = ""  # what the pin is ("Stripe account", "Teller application")
    script: str = ""  # the optional server fallback
    feature: str = ""  # the optional feature that uses it, if any
    generated: bool = False  # made in the portal, nothing typed (vendor key)

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields) + tuple(self.implied)


SECURITY_CHOICES = (("tls", "STARTTLS (port 587)"), ("ssl", "TLS from the start (port 465)"))

SERVICES: tuple[Service, ...] = (
    Service(
        "smtp2go",
        "Email through SMTP2Go",
        "The relay's HTTP API sends the portal's email and reports delivery and bounces.",
        (
            Field(
                "SMTP2GO_API_KEY",
                "SMTP2Go API key",
                SECRET,
                "Sending -> API Keys in SMTP2Go, with Emails, Activity and Statistics only. "
                "Starts with api-.",
                max_length=80,
            ),
            Field(
                "SMTP2GO_REGION",
                "Region",
                CHOICE,
                "The API host your account uses.",
                choices=(("global", "Global"), ("us", "US"), ("eu", "EU"), ("au", "Australia")),
            ),
            Field(
                "DEFAULT_FROM_EMAIL",
                "Sender",
                TEXT,
                'Name and address on the verified domain, e.g. "Oak Creek Farms HOA '
                '<portal@example.org>".',
            ),
        ),
        implied={"EMAIL_PROVIDER": "smtp2go"},
        script="set-smtp2go.sh",
    ),
    Service(
        "smtp",
        "Email through an SMTP server",
        "Any mail server with a login (also SMTP2Go's SMTP). Saving switches email to it.",
        (
            Field("EMAIL_HOST", "Server", TEXT, "e.g. smtp.example.org", max_length=253),
            Field("EMAIL_PORT", "Port", INT, "587 with STARTTLS, 465 with TLS."),
            Field("EMAIL_HOST_USER", "User name", TEXT, max_length=200),
            Field("EMAIL_HOST_PASSWORD", "Password", SECRET, max_length=200),
            Field("EMAIL_SECURITY", "Encryption", CHOICE, choices=SECURITY_CHOICES),
            Field("DEFAULT_FROM_EMAIL", "Sender", TEXT, 'e.g. "Oak Creek Farms HOA <portal@...>"'),
        ),
        implied={"EMAIL_PROVIDER": "smtp"},
    ),
    Service(
        "stripe",
        "Online payments (Stripe)",
        "A restricted key (rk_...) with the rights in the runbook; never a full secret key.",
        (
            Field(
                "STRIPE_SECRET_KEY",
                "Stripe restricted key",
                SECRET,
                "rk_test_... on the POC, rk_live_... in production.",
                max_length=255,
            ),
        ),
        implied={"PAYMENTS_PROVIDER": "stripe"},
        approval=True,
        pinned="Stripe account",
        script="set-stripe-key.sh",
        feature="online_payments",
    ),
    Service(
        "teller",
        "Bank feed (Teller)",
        "The application ID and the certificate and private key from Teller's dashboard.",
        (
            Field("TELLER_APPLICATION_ID", "Application ID", TEXT, "Starts with app_."),
            Field(
                "TELLER_ENVIRONMENT",
                "Environment",
                CHOICE,
                choices=(
                    ("sandbox", "Sandbox"),
                    ("development", "Development (free tier, real accounts)"),
                    ("production", "Production"),
                ),
            ),
            Field("TELLER_CERTIFICATE_B64", "Certificate (certificate.pem)", FILE, required=False),
            Field("TELLER_PRIVATE_KEY_B64", "Private key (private_key.pem)", FILE, required=False),
        ),
        approval=True,
        pinned="Teller application",
        script="set-teller.sh",
        feature="bank_feeds",
    ),
    Service(
        "usps",
        "USPS tracking",
        "The association's Business Customer Gateway login with IV-MTR access.",
        (
            Field(
                "USPS_IV_ENVIRONMENT",
                "Environment",
                CHOICE,
                choices=(("cat", "USPS test environment (CAT)"), ("production", "Production")),
            ),
            Field("USPS_IV_USERNAME", "BCG user name", TEXT, max_length=100),
            Field("USPS_IV_PASSWORD", "BCG password", SECRET, max_length=200),
        ),
        script="set-usps.sh",
        feature="usps_tracking",
    ),
    Service(
        "bounce",
        "Bounce mailbox (IMAP)",
        "Only for plain SMTP: with SMTP2Go, bounces come from its reports.",
        (
            Field("BOUNCE_IMAP_HOST", "IMAP server", TEXT, "IMAP over TLS.", max_length=253),
            Field("BOUNCE_IMAP_PORT", "Port", INT, "Usually 993."),
            Field("BOUNCE_IMAP_USER", "User name", TEXT),
            Field("BOUNCE_IMAP_PASSWORD", "Password or app password", SECRET),
            Field("BOUNCE_IMAP_FOLDER", "Folder", TEXT, "Usually INBOX.", max_length=100),
        ),
        script="set-bounce-mailbox.sh",
        feature="bounce_tracking",
    ),
    Service(
        "ai",
        "Reading signed forms (Gemini)",
        "Reads scanned neighbor sign-off forms; a person confirms what it reads.",
        (
            Field("GEMINI_API_KEY", "Gemini API key", SECRET, max_length=200),
            Field("GEMINI_MODEL", "Model", TEXT, "e.g. gemini-2.5-flash", max_length=80),
        ),
    ),
    Service(
        "vendor_key",
        "Vendor document key",
        "Encrypts vendors' W-9s and insurance certificates. Made here: nothing to type.",
        (
            Field("VENDOR_DOCUMENT_KEY", "Current key", GENERATED),
            Field(
                "VENDOR_DOCUMENT_KEY_FALLBACKS",
                "Older keys (still open old files)",
                LIST,
                required=False,
            ),
        ),
        script="set-vendor-key.sh",
        feature="vendor_onboarding",
        generated=True,
    ),
)
BY_KEY: dict[str, Service] = {s.key: s for s in SERVICES}
FIELDS: dict[str, Field] = {}
for _service in SERVICES:
    for _f in _service.fields:
        FIELDS.setdefault(_f.name, _f)
IMPLIED_NAMES = frozenset(n for s in SERVICES for n in s.implied)
# EMAIL_SECURITY is the form's choice; the portal reads these two.
DERIVED = {"EMAIL_USE_TLS": ("EMAIL_SECURITY", "tls"), "EMAIL_USE_SSL": ("EMAIL_SECURITY", "ssl")}
NAMES = frozenset(FIELDS) | IMPLIED_NAMES | frozenset(DERIVED)


# --- The key-encryption key -------------------------------------------------------------


def kek_configured() -> bool:
    return len(getattr(settings, "CREDENTIALS_KEK", "") or "") >= MIN_KEK_LENGTH


@sensitive_variables("kek")
def _data_key() -> bytes:
    kek = getattr(settings, "CREDENTIALS_KEK", "") or ""
    if len(kek) < MIN_KEK_LENGTH:
        raise CredentialError(
            "The server's key-encryption key (CREDENTIALS_KEK) is missing, so nothing can be "
            "saved here. It is created on the server at install (start-portal.sh)."
        )
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(kek.encode())


def key_id() -> str:
    """A keyed fingerprint of the current data key (not the key)."""
    return hmac.new(_data_key(), b"key id", hashlib.sha256).hexdigest()[:16]


def _aad(name: str, change_id: int) -> bytes:
    return f"service-credential:{name}:{change_id}".encode()


@sensitive_variables("value", "key")
def seal(value: str, name: str, change_id: int) -> bytes:
    key = _data_key()
    nonce = os.urandom(NONCE_BYTES)
    return FORMAT + nonce + AESGCM(key).encrypt(nonce, value.encode(), _aad(name, change_id))


class Unreadable(Exception):
    """A stored value no configured key opens (fails closed)."""


@sensitive_variables("key")
def unseal(blob: bytes, name: str, change_id: int) -> str:
    try:
        key = _data_key()
    except CredentialError as exc:
        raise Unreadable() from exc
    blob = bytes(blob)
    if not blob.startswith(FORMAT) or len(blob) < len(FORMAT) + NONCE_BYTES + 16:
        raise Unreadable()
    nonce = blob[len(FORMAT) : len(FORMAT) + NONCE_BYTES]
    try:
        return (
            AESGCM(key)
            .decrypt(nonce, blob[len(FORMAT) + NONCE_BYTES :], _aad(name, change_id))
            .decode()
        )
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise Unreadable() from exc


# --- Reading: the state of every change, cached briefly ---------------------------------

PENDING, WAITING, ACTIVE, SUPERSEDED, EXPIRED, REFUSED, CANCELLED = (
    "pending",
    "waiting",
    "active",
    "superseded",
    "expired",
    "refused",
    "cancelled",
)
STATE_WORDS = {
    PENDING: "Waiting for board approval",
    WAITING: "Approved; waiting period running",
    ACTIVE: "In use",
    SUPERSEDED: "Replaced by a newer change",
    EXPIRED: "Lapsed without approval",
    REFUSED: "Refused by the board",
    CANCELLED: "Cancelled",
}


@dataclass
class ChangeState:
    change: object
    state: str
    active_at: datetime.datetime | None
    approvers: list = field(default_factory=list)

    @property
    def words(self) -> str:
        return STATE_WORDS[self.state]


@dataclass
class Current:
    """What a name currently reads as."""

    value: object = None
    source: str = ""  # "portal", "server" or ""
    change: object = None
    last4: str = ""
    shown: str = ""
    unreadable: bool = False


@dataclass
class State:
    changes: list[ChangeState] = field(default_factory=list)
    values: dict[str, Current] = field(default_factory=dict)  # names saved in the portal
    pins: dict[str, str] = field(default_factory=dict)


_lock = threading.Lock()
_cache: list = [0.0, None]  # [expires (monotonic), State]
_candidate: ContextVar[dict | None] = ContextVar("credential_candidate", default=None)


def forget() -> None:
    """Drop this process's copy (after a save; tests)."""
    with _lock:
        _cache[0], _cache[1] = 0.0, None


def _evaluate(change, decisions, now) -> ChangeState:
    by_kind = {}
    for d in decisions:
        by_kind.setdefault(d.decision, []).append(d)
    if by_kind.get("cancel"):
        return ChangeState(change, CANCELLED, None)
    if by_kind.get("reject"):
        return ChangeState(change, REFUSED, None)
    approvals = [
        d
        for d in by_kind.get("approve", [])
        if d.by_id != change.requested_by_id
        and (change.expires_at is None or d.at <= change.expires_at)
    ]
    approvers = [d.by_id for d in approvals]
    if len(approvals) >= change.approvals_needed:
        last = max([d.at for d in approvals], default=change.requested_at)
        active_at = max(last, change.requested_at + datetime.timedelta(hours=change.wait_hours))
        state = ACTIVE if active_at <= now else WAITING
        return ChangeState(change, state, active_at, approvers)
    if change.expires_at and now > change.expires_at:
        return ChangeState(change, EXPIRED, None, approvers)
    return ChangeState(change, PENDING, None, approvers)


def _load(now=None) -> State:
    from core.models import CredentialChange, CredentialDecision, CredentialValue

    now = now or timezone.now()
    state = State()
    changes = list(CredentialChange.objects.order_by("requested_at", "id"))
    if not changes:
        return state
    decisions: dict[int, list] = {}
    for d in CredentialDecision.objects.order_by("at", "id"):
        decisions.setdefault(d.change_id, []).append(d)
    values: dict[int, list] = {}
    for v in CredentialValue.objects.order_by("id"):
        values.setdefault(v.change_id, []).append(v)
    newest: dict[str, int] = {}
    for c in changes:
        newest[c.service] = c.pk
    for c in changes:
        cs = _evaluate(c, decisions.get(c.pk, []), now)
        # A newer change for the service replaces one still waiting for approval.
        if cs.state == PENDING and newest[c.service] != c.pk:
            cs = ChangeState(c, SUPERSEDED, None, cs.approvers)
        state.changes.append(cs)
    active = sorted(
        (cs for cs in state.changes if cs.state == ACTIVE),
        key=lambda cs: (cs.active_at, cs.change.pk),
    )
    for cs in active:
        c = cs.change
        if c.account_ref and (c.service not in state.pins or c.kind == "pin_change"):
            state.pins[c.service] = c.account_ref
        for v in values.get(c.pk, []):
            if v.cleared:
                state.values.pop(v.name, None)
                continue
            cur = Current(source="portal", change=c, last4=v.last4, shown=v.shown)
            try:
                cur.value = unseal(v.ciphertext, v.name, c.pk)
            except Unreadable:
                cur.value, cur.unreadable = "", True
                logger.error("credentials.unreadable", extra={"setting": v.name})
            state.values[v.name] = cur
    return state


def state() -> State:
    ttl = max(0, int(getattr(settings, "CREDENTIALS_CACHE_SECONDS", 15)))
    with _lock:
        if _cache[1] is not None and time.monotonic() < _cache[0]:
            return _cache[1]
    try:
        loaded = _load()
    except (DatabaseError, RuntimeError):
        # No database to read (before the first migrate, or a process without
        # one, e.g. a test that does not use it): the server's settings, not
        # remembered, so the next read tries again.
        logger.warning("credentials.not_loaded")
        return State()
    with _lock:
        _cache[0], _cache[1] = time.monotonic() + ttl, loaded
    return loaded


def _convert(name: str, raw):
    kind = FIELDS[name].kind if name in FIELDS else TEXT
    if kind == INT:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if kind == BOOL:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == LIST:
        return [p for p in str(raw).split(",") if p]
    return raw


def _raw(name: str, st: State):
    over = _candidate.get()
    if over is not None and name in over:
        return True, over[name]
    cur = st.values.get(name)
    if cur is not None:
        return True, cur.value
    return False, None


def get(name: str):
    """The setting ``name``: saved in the portal, else the server's settings.
    A saved value that cannot be opened reads as empty (fails closed)."""
    if name not in NAMES:
        raise KeyError(f"{name} is not a Keys and connections setting")
    st = state()
    if name in DERIVED:
        source, wanted = DERIVED[name]
        found, raw = _raw(source, st)
        if found:
            return str(raw) == wanted
        return getattr(settings, name, False)
    found, raw = _raw(name, st)
    if found:
        return _convert(name, raw)
    if name == "EMAIL_SECURITY":
        return _server_value(name)
    return getattr(settings, name, "" if FIELDS.get(name, Field(name, "")).kind != LIST else [])


def source(name: str) -> str:
    """Where ``name`` comes from: "portal", "server" (a non-empty setting) or ""."""
    if name in state().values:
        return "portal"
    if name in DERIVED:
        return "server"
    return "server" if getattr(settings, name, "") else ""


@contextlib.contextmanager
def candidate(values: dict):
    """Within the block, ``get`` reads ``values`` first: the test before a
    save runs the portal's own code against what was typed, nothing stored."""
    token = _candidate.set(dict(values))
    try:
        yield
    finally:
        _candidate.reset(token)


def pin(service: str) -> str:
    return state().pins.get(service, "")


# --- Writing ------------------------------------------------------------------------------


def last4(value: str) -> str:
    value = (value or "").strip()
    return value[-4:] if len(value) >= 12 else ""


def _rate_limited(actor) -> bool:
    from core.models import CredentialChange

    since = timezone.now() - datetime.timedelta(hours=1)
    return (
        CredentialChange.objects.filter(requested_by=actor, requested_at__gte=since).count()
        >= SAVES_PER_HOUR
    )


def note_test(actor) -> bool:
    """Count a test for the rate limit; False once over it."""
    from django.core.cache import cache

    key = f"credentials:tests:{getattr(actor, 'pk', 0)}"
    cache.add(key, 0, timeout=3600)
    try:
        return cache.incr(key) <= TESTS_PER_HOUR
    except ValueError:
        return True


def _audit(action, actor, request=None, outcome="success", **metadata):
    from audit.services import record_event

    record_event(action, actor=actor, request=request, outcome=outcome, metadata=metadata)


def _require(actor, action):
    from core import policy

    policy.require(actor, action)


@sensitive_variables("values", "clean")
def save(
    actor,
    service_key: str,
    values: dict,
    *,
    test=None,
    saved_anyway: bool = False,
    pin_change: bool = False,
    request=None,
):
    """Record a change of ``service_key``'s settings. ``values``: name ->
    text (secrets left blank keep the saved one). ``test``: the TestResult
    run on them just before. Returns the ``CredentialChange``."""
    from core.models import CredentialChange, CredentialValue

    _require(actor, "system.manage_keys")
    service = BY_KEY.get(service_key)
    if service is None:
        raise CredentialError("Unknown service.")
    if not kek_configured():
        raise CredentialError(
            "The server's key-encryption key (CREDENTIALS_KEK) is missing: nothing can be saved "
            "here until start-portal.sh has added it."
        )
    if _rate_limited(actor):
        _audit(
            "system.credential_refused",
            actor,
            request,
            "denied",
            service=service_key,
            reason="rate_limited",
        )
        raise CredentialError("Too many changes in the last hour. Try again later.")
    if test is not None and not test.ok and not saved_anyway:
        raise CredentialError("The test failed, so nothing was saved: " + test.words)
    clean = {k: v for k, v in values.items() if k in service.names and v not in (None, "")}
    clean.update(service.implied)
    if not any(k for k in clean if k not in service.implied):
        raise CredentialError("Nothing to save: enter at least one value.")
    account = (test.account_ref if test is not None else "") or ""
    current_pin = pin(service_key)
    if service.pinned:
        if pin_change:
            if not current_pin:
                raise CredentialError("No account is pinned yet: save the key normally.")
            if not account or account == current_pin:
                raise CredentialError("This key belongs to the pinned account: save it normally.")
        elif current_pin and account != current_pin:
            _audit(
                "system.credential_refused",
                actor,
                request,
                "denied",
                service=service_key,
                reason="other_account",
            )
            raise CredentialError(
                f"This key belongs to a different {service.pinned.lower()} than the pinned one, "
                "so it was refused. Changing the pinned account needs two board members and a "
                f'{PIN_CHANGE_WAIT_HOURS}-hour wait: tick "Change the pinned account".'
            )
        elif not account:
            raise CredentialError(
                f"The test did not say which {service.pinned.lower()} the key belongs to, so it "
                "cannot be pinned. Run the test (it must pass) and save again."
            )
    now = timezone.now()
    needed, wait, expires = 0, 0, None
    if pin_change:
        needed, wait = PIN_CHANGE_APPROVALS, PIN_CHANGE_WAIT_HOURS
        expires = now + datetime.timedelta(days=PIN_CHANGE_EXPIRY_DAYS)
    elif service.approval:
        needed, expires = 1, now + datetime.timedelta(hours=APPROVAL_HOURS)
    with transaction.atomic():
        change = CredentialChange.objects.create(
            service=service_key,
            kind=CredentialChange.Kind.PIN_CHANGE if pin_change else CredentialChange.Kind.SET,
            requested_by=actor if getattr(actor, "pk", None) else None,
            requested_at=now,
            approvals_needed=needed,
            wait_hours=wait,
            expires_at=expires,
            account_ref=account[:64],
            test_ok=None if test is None else bool(test.ok),
            test_code=(test.code if test is not None else "")[:40],
            saved_anyway=bool(saved_anyway and test is not None and not test.ok),
        )
        kid = key_id()
        for name, value in sorted(clean.items()):
            fld = FIELDS.get(name)
            is_secret = bool(fld and fld.secret)
            CredentialValue.objects.create(
                change=change,
                name=name,
                ciphertext=seal(str(value), name, change.pk),
                last4=last4(str(value)) if is_secret and fld.kind != FILE else "",
                shown="" if is_secret else str(value)[:200],
                key_id=kid,
            )
        _audit(
            "system.credential_saved",
            actor,
            request,
            service=service_key,
            change_id=change.pk,
            names=",".join(sorted(clean))[:200],
            waits_for_board=needed > 0,
            tested=test is not None,
            test_ok=None if test is None else bool(test.ok),
            saved_anyway=change.saved_anyway,
            pin_change=pin_change,
        )
        transaction.on_commit(forget)
        transaction.on_commit(lambda: _tell_board_saved(change, service, actor))
    forget()
    return change


@sensitive_variables("new", "current", "fallbacks")
def make_vendor_key(actor, *, rotate: bool = False, request=None):
    """Make the vendor document key in the portal (D79, D93): 48 random bytes,
    nothing typed or shown. ``rotate`` keeps the current key (and the older
    ones) as fallbacks and re-encrypts stored documents in the background."""
    import secrets

    current = get("VENDOR_DOCUMENT_KEY") or ""
    if current and not rotate:
        raise CredentialError("A vendor document key is already set: use Rotate to replace it.")
    if rotate and not current:
        raise CredentialError("There is no key to rotate yet: make one first.")
    new = secrets.token_urlsafe(48)
    fallbacks = []
    for old in [current, *(get("VENDOR_DOCUMENT_KEY_FALLBACKS") or [])]:
        if old and old not in fallbacks and old != new:
            fallbacks.append(old)
    values = {"VENDOR_DOCUMENT_KEY": new}
    if fallbacks:
        values["VENDOR_DOCUMENT_KEY_FALLBACKS"] = ",".join(fallbacks)
    change = save(actor, "vendor_key", values, request=request)
    _audit("system.vendor_key_made", actor, request, change_id=change.pk, rotated=rotate)
    if rotate:

        def queue():
            from core.tasks import reencrypt_vendor_documents

            reencrypt_vendor_documents.defer()

        transaction.on_commit(queue)
    return change


def clear(actor, service_key: str, *, request=None):
    """Remove the portal's values for a service: the server's .env applies again."""
    from core.models import CredentialChange, CredentialValue

    _require(actor, "system.manage_keys")
    service = BY_KEY.get(service_key)
    if service is None:
        raise CredentialError("Unknown service.")
    if _rate_limited(actor):
        raise CredentialError("Too many changes in the last hour. Try again later.")
    with transaction.atomic():
        change = CredentialChange.objects.create(
            service=service_key, kind=CredentialChange.Kind.CLEAR, requested_by=actor
        )
        for name in service.names:
            CredentialValue.objects.create(change=change, name=name, cleared=True)
        _audit(
            "system.credential_cleared", actor, request, service=service_key, change_id=change.pk
        )
        transaction.on_commit(forget)
        transaction.on_commit(
            lambda: _tell_board(
                f"{service.label}: the keys saved in the portal were removed",
                f"{_name(actor)} removed the keys saved in the portal for {service.label}. The "
                "server's own settings, if any, apply again.",
                change,
            )
        )
    forget()
    return change


def change_state(change) -> ChangeState:
    for cs in state().changes:
        if cs.change.pk == change.pk:
            return cs
    forget()
    for cs in state().changes:
        if cs.change.pk == change.pk:
            return cs
    raise CredentialError("Unknown change.")


def decide(actor, change, decision: str, *, request=None):
    """A board member approves or refuses a waiting change, or its requester
    cancels it. Nobody approves their own change (D65)."""
    from core import conflicts
    from core.models import CredentialDecision

    forget()
    cs = change_state(change)
    if cs.state != PENDING:
        raise CredentialError(f"This change is not waiting for a decision ({cs.words.lower()}).")
    service = BY_KEY[change.service]
    if decision == "cancel":
        if getattr(actor, "pk", None) != change.requested_by_id:
            raise CredentialError("Only the person who made the change can cancel it.")
        _require(actor, "system.manage_keys")
    elif decision in ("approve", "reject"):
        _require(actor, "system.approve_keys")
        conflicts.refuse(
            actor,
            change,
            area="credentials",
            action=decision,
            request=request,
            error=CredentialError,
            message="You made this change, so another board member has to decide on it.",
        )
        if CredentialDecision.objects.filter(change=change, by=actor).exists():
            raise CredentialError("You have already decided on this change.")
    else:
        raise CredentialError("Unknown decision.")
    with transaction.atomic():
        row = CredentialDecision.objects.create(change=change, decision=decision, by=actor)
        _audit(
            "system.credential_decided",
            actor,
            request,
            service=change.service,
            change_id=change.pk,
            decision=decision,
        )
        transaction.on_commit(forget)
    forget()
    after = change_state(change)
    words = {"approve": "approved", "reject": "refused", "cancel": "cancelled"}[decision]
    body = (
        f"{_name(actor)} {words} the change of {service.label} made by "
        f"{_name(change.requested_by)} on {_when(change.requested_at)}."
    )
    if after.state == ACTIVE:
        body += " It is now in use."
    elif after.state == WAITING:
        body += f" It takes effect on {_when(after.active_at)} (the waiting period)."
    elif after.state == PENDING and decision == "approve":
        body += " It needs one more board member's approval."
    _tell_board(f"{service.label}: change {words}", body, change)
    return row


# --- Board notices (names only, never a value) --------------------------------------------


def _name(user) -> str:
    if user is None:
        return "The system"
    try:
        from core.people import display_name

        return display_name(user)
    except Exception:  # a name is only a courtesy here
        return "A sysadmin"


def _when(at) -> str:
    return timezone.localtime(at).strftime("%b %-d, %Y at %-I:%M %p")


def _tell_board(subject: str, body: str, change) -> None:
    from django.urls import reverse

    from comms.notify import mail_board
    from community.services import board_emails

    link = settings.PORTAL_BASE_URL + reverse("keys_change", args=[change.pk])
    try:
        mail_board(board_emails(), subject=subject, body=f"{body}\n\nSee the change: {link}")
    except Exception as exc:  # the change stands; only the notice failed
        logger.warning("credentials.board_not_told", extra={"exc_type": type(exc).__name__})


def _tell_board_saved(change, service, actor) -> None:
    names = ", ".join(sorted(f.label for f in service.fields if _in_change(change, f.name)))
    subject = f"Key changed: {service.label}"
    body = (
        f"{service.label} ({names}) was changed by {_name(actor)} on {_when(change.requested_at)}."
    )
    if change.saved_anyway:
        body += " Its test failed and it was saved anyway."
    if change.kind == "pin_change":
        subject = f"Approval needed: change of the pinned {service.pinned.lower()}"
        body += (
            f" It moves the pinned {service.pinned.lower()} to one ending "
            f"{change.account_ref[-4:]}. It needs {PIN_CHANGE_APPROVALS} board members' approval "
            f"(not the person who made it) within {PIN_CHANGE_EXPIRY_DAYS} days and then takes "
            f"effect {PIN_CHANGE_WAIT_HOURS} hours after it was made at the earliest. If you did "
            "not expect this, refuse it."
        )
    elif change.approvals_needed:
        subject = f"Approval needed: {service.label} key"
        body += (
            f" It takes effect once another board member approves it, within {APPROVAL_HOURS} "
            "hours; otherwise it lapses."
        )
    _tell_board(subject, body, change)


def _in_change(change, name) -> bool:
    return change.values.filter(name=name).exists()


# --- What the page shows ------------------------------------------------------------------


def overview(service: Service) -> dict:
    st = state()
    rows = []
    for f in service.fields:
        cur = st.values.get(f.name)
        src = "portal" if cur else ("server" if _server_value(f.name) else "")
        rows.append(
            {
                "field": f,
                "set": bool(cur and not cur.unreadable) or src == "server",
                "source": src,
                "unreadable": bool(cur and cur.unreadable),
                "last4": cur.last4 if cur else "",
                "shown": (cur.shown if cur else ("" if f.secret else _server_shown(f))),
                "change": cur.change if cur else None,
            }
        )
    history = [cs for cs in st.changes if cs.change.service == service.key]
    history.reverse()
    last_test = next((cs.change for cs in history if cs.change.test_ok is not None), None)
    return {
        "service": service,
        "rows": rows,
        "configured": all(r["set"] for r in rows if r["field"].required),
        "any_portal": any(r["source"] == "portal" for r in rows),
        "unreadable": any(r["unreadable"] for r in rows),
        "pending": [cs for cs in history if cs.state in (PENDING, WAITING)],
        "history": history[:10],
        "last_test": last_test,
        "pin": st.pins.get(service.key, ""),
    }


def _server_value(name: str):
    if name == "EMAIL_SECURITY":
        if getattr(settings, "EMAIL_USE_SSL", False):
            return "ssl"
        return "tls" if getattr(settings, "EMAIL_USE_TLS", False) else ""
    return getattr(settings, name, "")


def _server_shown(f: Field) -> str:
    value = _server_value(f.name)
    return str(value)[:200] if value not in (None, "") else ""


def status(service_key: str) -> dict:
    """Configured or not, for the setup wizard (board): no values."""
    service = BY_KEY[service_key]
    info = overview(service)
    return {
        "label": service.label,
        "configured": info["configured"],
        "in_portal": info["any_portal"],
        "pending": bool(info["pending"]),
        "unreadable": info["unreadable"],
    }

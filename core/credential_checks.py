"""The checks behind Keys and connections (D93): the format of what was typed
and a harmless test against the provider, run on the typed values (through
``credentials.candidate``) before anything is saved. Nothing here sends an
email, moves money or reads a message; no value reaches a log or the result.

Each test returns a ``TestResult``: ok, a short code, plain words and, for
Stripe and Teller, the provider account the key belongs to (the pin)."""

from __future__ import annotations

import base64
import binascii
import datetime
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from core import credentials

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TestResult:
    ok: bool
    code: str
    words: str
    account_ref: str = ""


def passed(words: str, account_ref: str = "") -> TestResult:
    return TestResult(True, "ok", words, account_ref)


def failed(code: str, words: str) -> TestResult:
    return TestResult(False, code[:40], words)


_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_SENDER = re.compile(r"^[^<>\r\n]{0,80}<?[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+>?$")
# The mail server and bounce mailbox tests connect from inside the portal, so
# they reach public mail hosts only (review EXP-004, 0.44.0): no IP address,
# no internal name, the usual ports only, and a name must resolve to public
# addresses alone.
_INTERNAL_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".home.arpa", ".localdomain")
PORTS = {
    "EMAIL_PORT": frozenset({25, 465, 587, 2525}),
    "BOUNCE_IMAP_PORT": frozenset({993}),
}


def host_problem(host: str) -> str:
    """Why ``host`` may not be used as a mail or IMAP server ("" when it may)."""
    host = (host or "").strip().lower().rstrip(".")
    if not _HOST.match(host):
        return "is not a host name"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "must be a host name, not an IP address"
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES) or host.split(".")[-1].isdigit():
        return "must be a public host name"
    return ""


# Replaced in tests: host -> the addresses it resolves to.
def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


resolve = _resolve


def public_addresses(host: str) -> list[str]:
    """The addresses ``host`` resolves to, if it resolves and only to public
    (global) ones; [] otherwise. Resolved once: the tests then connect to
    one of these very addresses (:func:`pinned_smtp`, :func:`pinned_imap`),
    so a second lookup cannot switch the connection to an internal one (DNS
    rebinding, review BUG-004 / EXP-003, 0.45.0)."""
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError):
        return []
    if not addresses:
        return []
    checked = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return []
        if not ip.is_global or ip.is_multicast:
            return []
        checked.append(str(ip))
    return checked


def public_host(host: str) -> bool:
    """``host`` resolves, and only to public (global) addresses."""
    return bool(public_addresses(host))


def _connect(address: str, port: int, timeout, source_address=None) -> socket.socket:
    if timeout is not None and not timeout:
        raise ValueError("Non-blocking socket (timeout=0) is not supported")
    return socket.create_connection((address, port), timeout, source_address)


def pinned_smtp(address: str, *, ssl: bool):
    """smtplib's SMTP (or SMTP_SSL) class that connects to the checked
    ``address`` while keeping the host name for TLS (SNI and certificate
    checks, STARTTLS included: smtplib uses the name it was given)."""
    import smtplib

    base = smtplib.SMTP_SSL if ssl else smtplib.SMTP

    class PinnedSMTP(base):
        def _get_socket(self, host, port, timeout):
            sock = _connect(address, port, timeout, self.source_address)
            if ssl:
                sock = self.context.wrap_socket(sock, server_hostname=self._host)
            return sock

    return PinnedSMTP


def pinned_imap(address: str):
    """imaplib.IMAP4_SSL that connects to the checked ``address``; TLS
    verifies the host name it was given."""
    import imaplib

    class PinnedIMAP(imaplib.IMAP4_SSL):
        def _create_socket(self, timeout):
            sock = _connect(address, self.port, timeout)
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)

    return PinnedIMAP


# --- Format checks (what was typed) -------------------------------------------------------


@sensitive_variables("values")
def validate(service_key: str, values: dict, *, merged: dict) -> list[str]:
    """Problems with the typed values, in words (never the values).
    ``merged``: the typed values over what is saved now."""
    problems: list[str] = []
    service = credentials.BY_KEY[service_key]
    for f in service.fields:
        v = values.get(f.name)
        if v in (None, ""):
            continue
        if f.kind == credentials.CHOICE and v not in {c for c, _ in f.choices}:
            problems.append(f"Choose one of the offered values for {f.label}.")
        if f.kind == credentials.INT and not (str(v).isdigit() and 1 <= int(v) <= 65535):
            problems.append(f"{f.label} must be a port number.")
        elif f.name in PORTS and int(v) not in PORTS[f.name]:
            allowed = ", ".join(str(p) for p in sorted(PORTS[f.name]))
            problems.append(f"{f.label} must be one of {allowed}.")
        if isinstance(v, str) and len(v) > f.max_length and f.kind != credentials.FILE:
            problems.append(f"{f.label} is too long.")
        if isinstance(v, str) and ("\n" in v or "\r" in v) and f.kind != credentials.FILE:
            problems.append(f"{f.label} may not contain a line break.")
    key = values.get("SMTP2GO_API_KEY")
    if key:
        from comms.smtp2go import KEY_RE

        if not KEY_RE.match(key):
            problems.append("That is not an SMTP2Go API key (api- and letters/digits).")
    if values.get("DEFAULT_FROM_EMAIL") and not _SENDER.match(values["DEFAULT_FROM_EMAIL"]):
        problems.append('The sender must look like "Name <address@domain>".')
    for name in ("EMAIL_HOST", "BOUNCE_IMAP_HOST"):
        problem = host_problem(values[name]) if values.get(name) else ""
        if problem:
            problems.append(f"{credentials.FIELDS[name].label} {problem}.")
    retype = credentials.secrets_to_retype(service_key, values)
    if retype:
        problems.append(credentials._retype_words(retype))
    stripe = values.get("STRIPE_SECRET_KEY")
    if stripe:
        if stripe.startswith("sk_"):
            problems.append(
                "That is a full secret key (sk_...). Only a restricted key (rk_...) with the "
                'rights in the runbook ("Online payments (Stripe)") is accepted.'
            )
        elif not re.match(r"^rk_(test|live)_[A-Za-z0-9]{10,}$", stripe):
            problems.append("That is not a Stripe restricted key (rk_...).")
        elif stripe.startswith("rk_live_") and settings.DEPLOYMENT_STAGE != "production":
            problems.append("This is not the production portal: use a test key (rk_test_...).")
        elif stripe.startswith("rk_test_") and settings.DEPLOYMENT_STAGE == "production":
            problems.append("Test keys are refused in production.")
    app = values.get("TELLER_APPLICATION_ID")
    if app and not re.match(r"^app_[A-Za-z0-9]{6,}$", app):
        problems.append("That does not look like a Teller Application ID (app_...).")
    if service_key == "teller":
        problems += _teller_files(merged)
    model = values.get("GEMINI_MODEL")
    if model and not re.match(r"^[a-z0-9][a-z0-9.-]{1,79}$", model):
        problems.append("The model name may use lower-case letters, digits, dots and dashes.")
    return problems


@sensitive_variables("merged", "cert", "key")
def _teller_files(merged: dict) -> list[str]:
    env = merged.get("TELLER_ENVIRONMENT") or "sandbox"
    cert_b64, key_b64 = merged.get("TELLER_CERTIFICATE_B64"), merged.get("TELLER_PRIVATE_KEY_B64")
    if env == "sandbox" and not (cert_b64 or key_b64):
        return []
    if not (cert_b64 and key_b64):
        return ["Outside the sandbox Teller needs both the certificate and the private key."]
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert = x509.load_pem_x509_certificate(base64.b64decode(cert_b64, validate=True))
    except (ValueError, binascii.Error):
        return ["The certificate file is not a PEM certificate."]
    try:
        key = serialization.load_pem_private_key(base64.b64decode(key_b64, validate=True), None)
    except (ValueError, TypeError, binascii.Error):
        return ["The private key file is not an unencrypted PEM private key."]
    fmt = serialization.PublicFormat.SubjectPublicKeyInfo
    enc = serialization.Encoding.DER
    if cert.public_key().public_bytes(enc, fmt) != key.public_key().public_bytes(enc, fmt):
        return ["The certificate and the private key do not belong together."]
    if cert.not_valid_after_utc <= datetime.datetime.now(datetime.UTC):
        return ["The certificate has expired: create a new one in Teller's dashboard."]
    return []


def pem_upload_b64(upload, *, max_bytes: int = 20_000) -> str:
    """A small uploaded PEM file as base64 (as TELLER_*_B64 holds it).
    Read in the request, never stored as a file."""
    if upload is None:
        return ""
    if upload.size > max_bytes:
        raise credentials.CredentialError("That file is too large for a certificate or key.")
    data = upload.read(max_bytes + 1)
    if b"-----BEGIN" not in data:
        raise credentials.CredentialError("Upload the .pem files from Teller's dashboard.")
    return base64.b64encode(data).decode()


# --- Tests against the provider -----------------------------------------------------------


def _smtp2go() -> TestResult:
    from comms import smtp2go

    cfg = smtp2go.config()
    if not cfg.configured:
        return failed(cfg.code or "not_configured", cfg.problem)
    try:
        smtp2go.check_key()
    except smtp2go.Smtp2goError as exc:
        return failed(exc.code, f"SMTP2Go: {exc.words}.")
    return passed(f"SMTP2Go ({cfg.host}) accepted the key; no email was sent.")


def _smtp() -> TestResult:
    import smtplib

    from django.core.mail.backends.smtp import EmailBackend

    from core.email import smtp_options

    options = smtp_options()
    if not options["host"]:
        return failed("not_configured", "Enter the SMTP server.")
    addresses = [] if host_problem(options["host"]) else public_addresses(options["host"])
    if not addresses:
        return failed("not_public", "The mail server must be a public host name that resolves.")
    try:
        backend = _pinned_backend(EmailBackend, addresses[0])(
            fail_silently=False, timeout=20, **options
        )
        backend.open()  # connect, encrypt and sign in; nothing is sent
        backend.close()
    except smtplib.SMTPAuthenticationError:
        return failed("auth", "The mail server refused the user name or password.")
    except ValueError:
        return failed("bad_security", "Choose STARTTLS or TLS, not both.")
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("credentials.smtp_test_failed", extra={"exc_type": type(exc).__name__})
        return failed("connect", "The mail server could not be reached or refused TLS.")
    return passed(f"Signed in to {options['host']}:{options['port']}; nothing was sent.")


def _pinned_backend(backend_class, address: str):
    """Django's SMTP backend, connecting only to the checked address."""
    if not isinstance(backend_class, type):  # replaced in tests
        return backend_class

    class PinnedBackend(backend_class):
        @property
        def connection_class(self):
            return pinned_smtp(address, ssl=bool(self.use_ssl))

    return PinnedBackend


def _stripe() -> TestResult:
    from payments import providers, stripe_api

    cfg = providers.config()
    if not cfg.available:
        return failed("not_usable", cfg.problem)
    try:
        account = stripe_api.get("/v1/account", what="keys_account")
        stripe_api.get("/v1/balance", what="keys_balance")
    except stripe_api.StripeError as exc:
        if exc.status == 0:
            return failed("network", "Stripe could not be reached from the server.")
        if exc.status == 401:
            return failed("auth", "Stripe refused the key (wrong or revoked).")
        return failed(
            f"http_{exc.status}",
            "The key lacks a read right (Account and Balance read are needed; see the "
            f'runbook, "Online payments"). Stripe answered HTTP {exc.status}.',
        )
    acct = account.get("id") if isinstance(account, dict) else ""
    if not (isinstance(acct, str) and re.match(r"^acct_[A-Za-z0-9]{6,60}$", acct)):
        return failed("no_account", "Stripe did not say which account the key belongs to.")
    return passed(f"Stripe accepted the key ({cfg.mode_label}); account ending {acct[-4:]}.", acct)


def _usps() -> TestResult:
    from comms import usps_iv

    cfg = usps_iv.config()
    if not cfg.configured:
        return failed("not_configured", cfg.problem)
    try:
        usps_iv.check_login()
    except usps_iv.UspsAuthError:
        return failed("auth", "USPS refused the sign-in (user name, password, IV-MTR access).")
    except usps_iv.UspsRateLimited:
        return failed("rate_limited", "USPS said too many requests; try again in a minute.")
    except usps_iv.UspsError as exc:
        if exc.status == 0:
            return failed("network", f"USPS ({cfg.auth_host}) could not be reached.")
        return failed(f"http_{exc.status}", f"USPS answered with an error (HTTP {exc.status}).")
    finally:
        usps_iv.forget_token()
    return passed(f"Signed in to {cfg.label}; no mail piece was looked up.")


# Replaced in tests: (url, ssl context) -> HTTP status, 0 when the TLS
# handshake or the connection failed.
def _teller_transport(url: str, context) -> int:
    from ledger.bankfeeds.teller import build_opener

    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with build_opener(context).open(request, timeout=20) as response:  # nosec B310
            return getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


teller_transport = _teller_transport


def _teller() -> TestResult:
    from ledger.bankfeeds import teller
    from ledger.bankfeeds.base import BankFeedError

    app = teller.application_id()
    if not app:
        return failed("not_configured", "Enter the Teller Application ID.")
    try:
        env = teller.environment()
        context = teller.ssl_context()
    except BankFeedError as exc:
        return failed("certificate", str(exc))
    # No access token: Teller answers 401 once the client certificate is
    # accepted, and refuses the connection (or 400/403) when it is not.
    status = teller_transport(f"{teller.API_BASE}/accounts", context)
    if status == 0:
        return failed("network", "Teller could not be reached, or refused the certificate.")
    if status in (400, 403, 495, 496):
        return failed("certificate", f"Teller refused the certificate (HTTP {status}).")
    if status not in (200, 401):
        return failed(f"http_{status}", f"Teller answered HTTP {status}.")
    return passed(f"Teller accepted the connection ({env}).", app)


def _bounce() -> TestResult:
    from comms import bounces

    cfg = bounces.config()
    if not cfg.configured:
        return failed("not_configured", cfg.problem)
    addresses = [] if host_problem(cfg.host) else public_addresses(cfg.host)
    if not addresses:
        return failed("not_public", "The IMAP server must be a public host name that resolves.")
    try:
        bounces.check_login(factory=pinned_imap(addresses[0]))
    except bounces.MailboxError as exc:
        words = {
            "auth": "The mailbox refused the sign-in.",
            "connect": f"{cfg.host}:{cfg.port} could not be reached.",
            "tls": f"{cfg.host}'s certificate did not check out.",
            "folder": "Signed in, but the folder was not found.",
        }.get(exc.code, f"The check failed ({exc.code}).")
        return failed(exc.code, words)
    return passed(f"Signed in to {cfg.host}:{cfg.port} over TLS; no message was read.")


def _ai() -> TestResult:
    from core import ai

    try:
        ai.ping()
    except ai.AIError as exc:
        return failed("refused", str(exc))
    return passed("Gemini accepted the key; nothing was sent to it.")


def _vendor_key() -> TestResult:
    from operations import vendor_crypto

    if not vendor_crypto.configured():
        return failed("not_configured", "No vendor document key.")
    probe = vendor_crypto.encrypt(b"vendor key check", "probe")
    if vendor_crypto.decrypt(probe, "probe") != b"vendor key check":
        return failed("round_trip", "The key does not round-trip.")
    return passed("A test value was encrypted and opened.")


TESTERS = {
    "smtp2go": _smtp2go,
    "smtp": _smtp,
    "stripe": _stripe,
    "usps": _usps,
    "teller": _teller,
    "bounce": _bounce,
    "ai": _ai,
    "vendor_key": _vendor_key,
}


@sensitive_variables("values", "merged")
def run(service_key: str, values: dict) -> TestResult:
    """Run the service's test on ``values`` over what is in use now. Never
    raises for a provider problem; the result says what failed."""
    service = credentials.BY_KEY[service_key]
    merged = {}
    for name in service.names:
        if name in credentials.FIELDS:
            current = credentials.get(name)
            if current not in (None, "", []):
                merged[name] = ",".join(current) if isinstance(current, list) else current
    merged.update({k: v for k, v in values.items() if v not in (None, "")})
    merged.update(service.implied)
    problems = validate(service_key, values, merged=merged)
    if problems:
        return failed("format", " ".join(problems))
    return _run_tester(service_key, merged)


@sensitive_variables("values")
def _run_tester(service_key: str, values: dict) -> TestResult:
    try:
        with credentials.candidate(values):
            return TESTERS[service_key]()
    except Exception as exc:  # a test must never show a value or fail the page
        logger.warning("credentials.test_error", extra={"exc_type": type(exc).__name__})
        return failed("error", f"The test could not run ({type(exc).__name__}).")


def run_server(service_key: str) -> TestResult:
    """The service's test on the server's own .env settings only (what would
    apply if the portal's values were removed)."""
    return _run_tester(service_key, credentials.server_values(service_key))

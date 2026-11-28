"""A small USPS Informed Visibility (IV-MTR) client on urllib (D60).

Reads the scans of one barcoded mailpiece by its 31-digit Intelligent Mail
barcode. Pull only: nothing USPS sends to us, no webhooks.

* Hosts: USPS's own for the configured environment (``USPS_IV_ENVIRONMENT``
  ``cat``: qiv.usps.com and cat-services.usps.com; ``production``:
  iv.usps.com and services.usps.com). ``USPS_IV_API_HOST`` and
  ``USPS_IV_AUTH_HOST`` may override them, but only with *.usps.com names;
  every request is refused unless it goes over https to exactly one of the
  two configured hosts.
* Python's default certificate verification; redirects are refused (urllib
  would otherwise copy the Authorization header to whatever host a 30x
  names); 15 second timeouts.
* Sign-in: the BCG user name and password from the server's .env (put there
  by infra/poc/set-usps.sh) are posted to /oauth/authenticate for a
  15-minute access token and a refresh token. Both are kept in this
  process's memory only, never in the database, the cache table or a log.
* Requests are paced to at most ``MAX_PER_MINUTE`` a minute (USPS allows
  500 per CRID).
* Errors are reported by HTTP status and a short code only. Request and
  response bodies, the login, tokens and barcodes are never logged.

Tests replace ``transport`` (and ``sleep``/``clock``); nothing in the test
suite reaches the network.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("comms.usps")

ENVIRONMENTS = {
    "cat": ("qiv.usps.com", "cat-services.usps.com"),
    "production": ("iv.usps.com", "services.usps.com"),
}
ENVIRONMENT_LABELS = {
    "cat": "the USPS test environment (CAT)",
    "production": "USPS Informed Visibility",
}
API_PATH = "/ivws_api/informedvisapi"
# The public client id USPS publishes for IV-MTR API sign-in (not a secret).
CLIENT_ID = "687b8a36-db61-42f7-83f7-11c79bf7785e"
SCOPE = "user.info.ereg,iv1.apis"
TIMEOUT_SECONDS = 15
MAX_PER_MINUTE = 400
# Renew the access token this long before USPS says it expires.
TOKEN_MARGIN_SECONDS = 60
DEFAULT_TOKEN_SECONDS = 900
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+usps\.com$")
_IMB = re.compile(r"^[0-9]{20}(?:[0-9]{5}|[0-9]{9}|[0-9]{11})?$")
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~+/=-]{8,8192}$")


class UspsError(RuntimeError):
    """A USPS request that did not work: HTTP status (0 = none) and a short code."""

    def __init__(self, status: int = 0, code: str = ""):
        super().__init__(f"USPS request failed (HTTP {status}{', ' + code if code else ''})")
        self.status = status
        self.code = code


class UspsAuthError(UspsError):
    """USPS refused the sign-in (wrong login, locked, or no IV-MTR access)."""


class UspsRateLimited(UspsError):
    """HTTP 429: too many requests; wait ``retry_after`` seconds."""

    def __init__(self, retry_after: int = 60):
        super().__init__(429, "rate_limited")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Config:
    environment: str
    api_host: str
    auth_host: str
    configured: bool
    problem: str = ""

    @property
    def label(self) -> str:
        return ENVIRONMENT_LABELS.get(self.environment, self.environment)


def valid_host(host: str) -> bool:
    return isinstance(host, str) and len(host) <= 253 and bool(_HOST.match(host))


def config() -> Config:
    """The environment and hosts from settings; ``configured`` once a login is set."""
    from core import credentials

    env = (credentials.get("USPS_IV_ENVIRONMENT") or "cat").strip().lower()
    if env not in ENVIRONMENTS:
        return Config(env, "", "", False, "USPS_IV_ENVIRONMENT must be cat or production.")
    api_default, auth_default = ENVIRONMENTS[env]
    api = (getattr(settings, "USPS_IV_API_HOST", "") or api_default).strip().lower()
    auth = (getattr(settings, "USPS_IV_AUTH_HOST", "") or auth_default).strip().lower()
    if not (valid_host(api) and valid_host(auth)):
        return Config(env, "", "", False, "USPS hosts must be usps.com names.")
    # The password reaches the worker container only (it polls USPS); the
    # web container is told that one is set, for the status lines (D87).
    password_set = bool(credentials.get("USPS_IV_PASSWORD")) or bool(
        getattr(settings, "USPS_IV_PASSWORD_SET", False)
    )
    if not (credentials.get("USPS_IV_USERNAME") and password_set):
        return Config(env, api, auth, False, "USPS_IV_USERNAME and USPS_IV_PASSWORD are not set.")
    return Config(env, api, auth, True)


# --- Transport ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the login and token must never reach another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(_NoRedirect())
    return _opener


def _allowed(url: str) -> bool:
    cfg = config()
    parts = urllib.parse.urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.port is None
        and not parts.username
        and parts.hostname in {cfg.api_host, cfg.auth_host}
        and bool(parts.hostname)
    )


def _transport(method: str, url: str, data: bytes | None, headers: dict) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with _get_opener().open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return response.status, response.read(2_000_000), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = b""
        with contextlib.suppress(Exception):
            body = exc.read(65536)
        return exc.code, body, dict(exc.headers or {})


# Replaced in tests with a fake USPS.
transport = _transport
sleep = time.sleep
clock = time.monotonic


class _Pacer:
    """At most ``MAX_PER_MINUTE`` requests a minute: an even gap between them."""

    def __init__(self):
        self.last: float | None = None
        self.lock = threading.Lock()

    def wait(self) -> None:
        gap = 60.0 / MAX_PER_MINUTE
        with self.lock:
            now = clock()
            if self.last is not None and now - self.last < gap:
                sleep(gap - (now - self.last))
                now = clock()
            self.last = now


pacer = _Pacer()


def _send(method: str, url: str, payload: dict | None, headers: dict, what: str):
    if not _allowed(url):
        logger.warning("comms.usps_error", extra={"what": what, "status": 0, "code": "bad_host"})
        raise UspsError(0, "bad_host")
    data = json.dumps(payload).encode() if payload is not None else None
    base = {"Accept": "application/json", "User-Agent": "hoa-portal-usps-tracking"}
    if data is not None:
        base["Content-Type"] = "application/json"
    pacer.wait()
    try:
        status, body, response_headers = transport(method, url, data, {**base, **headers})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("comms.usps_error", extra={"what": what, "status": 0, "code": "network"})
        raise UspsError(0, "network") from exc
    return status, body, response_headers or {}


def _json(body: bytes):
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return None


# --- Tokens (memory only) ----------------------------------------------------------------


@dataclass
class _Token:
    key: tuple = ()
    access: str = ""
    refresh: str = ""
    expires: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_token = _Token()


def forget_token() -> None:
    """Drop the tokens held in memory (tests; after a refused sign-in)."""
    _token.key, _token.access, _token.refresh, _token.expires = (), "", "", 0.0


def _token_key(cfg: Config) -> tuple:
    # Changing the environment, a host or the login starts afresh.
    from core import credentials

    return (cfg.environment, cfg.api_host, cfg.auth_host, credentials.get("USPS_IV_USERNAME"))


def _token_from(body: bytes, what: str) -> tuple[str, str, int]:
    data = _json(body)
    if isinstance(data, dict) and isinstance(data.get("data"), dict) and "access_token" not in data:
        data = data["data"]
    if not isinstance(data, dict):
        raise UspsError(200, "bad_response")
    access = data.get("access_token")
    refresh = data.get("refresh_token") or ""
    if not isinstance(access, str) or not _TOKEN_CHARS.match(access):
        logger.warning("comms.usps_error", extra={"what": what, "status": 200, "code": "no_token"})
        raise UspsError(200, "bad_response")
    if not isinstance(refresh, str) or (refresh and not _TOKEN_CHARS.match(refresh)):
        refresh = ""
    try:
        seconds = int(data.get("expires_in") or DEFAULT_TOKEN_SECONDS)
    except (TypeError, ValueError):
        seconds = DEFAULT_TOKEN_SECONDS
    return access, refresh, max(60, min(seconds, 24 * 3600))


def _auth_failure(status: int, what: str):
    logger.warning("comms.usps_error", extra={"what": what, "status": status, "code": "auth"})
    if status == 429:
        raise UspsRateLimited()
    if status in (400, 401, 403):
        raise UspsAuthError(status, "auth")
    raise UspsError(status, "server" if status >= 500 else "auth_failed")


def authenticate() -> None:
    """Sign in with the BCG login; keeps the tokens in memory."""
    cfg = config()
    if not cfg.configured:
        raise UspsError(0, "not_configured")
    from core import credentials

    password = credentials.get("USPS_IV_PASSWORD")
    if not password:
        # A container without the password (web, set on the server): only the worker signs in.
        raise UspsError(0, "not_configured")
    payload = {
        "username": credentials.get("USPS_IV_USERNAME"),
        "password": password,
        "grant_type": "authorization",
        "response_type": "token",
        "scope": SCOPE,
        "client_id": CLIENT_ID,
    }
    status, body, _ = _send(
        "POST", f"https://{cfg.auth_host}/oauth/authenticate", payload, {}, "authenticate"
    )
    del payload
    if not 200 <= status < 300:
        _auth_failure(status, "authenticate")
    access, refresh, seconds = _token_from(body, "authenticate")
    _token.key, _token.access, _token.refresh = _token_key(cfg), access, refresh
    _token.expires = clock() + seconds


def _refresh() -> bool:
    """Renew the access token with the refresh token; False if USPS refuses."""
    cfg = config()
    if not _token.refresh:
        return False
    payload = {
        "refresh_token": _token.refresh,
        "grant_type": "authorization",
        "response_type": "token",
        "scope": SCOPE,
    }
    status, body, _ = _send("POST", f"https://{cfg.auth_host}/oauth/token", payload, {}, "refresh")
    if not 200 <= status < 300:
        if status == 429:
            raise UspsRateLimited()
        if status >= 500:
            raise UspsError(status, "server")
        return False
    access, refresh, seconds = _token_from(body, "refresh")
    _token.access, _token.refresh = access, refresh or _token.refresh
    _token.expires = clock() + seconds
    return True


def access_token() -> str:
    """A valid access token: the one in memory, refreshed, or a new sign-in."""
    cfg = config()
    if not cfg.configured:
        raise UspsError(0, "not_configured")
    with _token.lock:
        if _token.key != _token_key(cfg):
            forget_token()
        if _token.access and clock() < _token.expires - TOKEN_MARGIN_SECONDS:
            return _token.access
        if not (_token.access and _refresh()):
            forget_token()
            authenticate()
        return _token.access


# --- Piece scans -------------------------------------------------------------------------


@dataclass(frozen=True)
class Scan:
    scanned_at: datetime.datetime
    opcode: str
    facility_city: str = ""
    facility_state: str = ""
    facility_zip: str = ""
    handling_event_type: str = ""
    mail_phase: str = ""


@dataclass(frozen=True)
class PieceResult:
    found: bool  # False: USPS has no scans for it (yet), HTTP 404
    not_ours: bool = False  # HTTP 403: not under our Mailer ID / CRID
    scans: tuple[Scan, ...] = ()
    skipped: int = 0  # scan entries that could not be read


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%Y%m%d%H%M%S",
)


def parse_time(value) -> datetime.datetime | None:
    """A scan time as IV writes it. Times without an offset are taken as the
    portal's time zone (USPS gives the facility's local time)."""
    if not isinstance(value, str) or not 8 <= len(value) <= 40:
        return None
    text = value.strip()
    parsed = None
    with contextlib.suppress(ValueError):
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed is None:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.datetime.strptime(text.split(".")[0], fmt)
                break
            except ValueError:
                continue
    if parsed is None or not 2000 <= parsed.year <= 2100:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed.replace(microsecond=0)


def _text(value, limit: int, pattern: str = r"[^A-Za-z0-9 .,'&/()#-]") -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return re.sub(pattern, "", str(value)).strip()[:limit]


def _scan(entry) -> Scan | None:
    if not isinstance(entry, dict):
        return None
    when = parse_time(entry.get("scan_date_time"))
    opcode = _text(entry.get("scan_event_code"), 4, r"[^0-9A-Za-z]")
    if when is None or not opcode:
        return None
    return Scan(
        scanned_at=when,
        opcode=opcode,
        facility_city=_text(entry.get("scan_facility_city"), 60),
        facility_state=_text(entry.get("scan_facility_state"), 2, r"[^A-Za-z]").upper(),
        facility_zip=_text(entry.get("scan_facility_zip"), 10, r"[^0-9-]"),
        handling_event_type=_text(entry.get("handling_event_type"), 4, r"[^0-9A-Za-z]"),
        mail_phase=_text(entry.get("mail_phase"), 80),
    )


def parse_piece(body: bytes) -> PieceResult:
    """The ``{"data": ..., "message": ...}`` envelope of a piece lookup.
    ``data`` is the piece (with ``scans``) or a list of pieces."""
    envelope = _json(body)
    if not isinstance(envelope, dict):
        raise UspsError(200, "bad_response")
    data = envelope.get("data")
    pieces = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    entries: list = []
    for piece in pieces:
        if isinstance(piece, dict) and isinstance(piece.get("scans"), list):
            entries.extend(piece["scans"][:500])
    scans, skipped = [], 0
    for entry in entries:
        scan = _scan(entry)
        if scan is None:
            skipped += 1
        else:
            scans.append(scan)
    return PieceResult(found=bool(scans), scans=tuple(scans), skipped=skipped)


def check_imb(imb: str) -> str:
    """A 20, 25, 29 or 31-digit Intelligent Mail barcode string."""
    if not isinstance(imb, str) or not imb.isascii() or not _IMB.match(imb):
        raise UspsError(0, "bad_imb")
    return imb


def piece_scans(imb: str) -> PieceResult:
    """The scans of one mailpiece. 404: none yet; 403: not ours (not asked
    again); 429: ``UspsRateLimited``; 401 once: signs in again and retries."""
    check_imb(imb)
    cfg = config()
    url = f"https://{cfg.api_host}{API_PATH}/api/mt/get/piece/imb/{imb}"
    for attempt in range(2):
        token = access_token()
        status, body, headers = _send(
            "GET", url, None, {"Authorization": f"Bearer {token}"}, "piece"
        )
        if status == 401 and attempt == 0:
            with _token.lock:
                forget_token()
            continue
        break
    if 200 <= status < 300:
        return parse_piece(body)
    if status == 404:
        return PieceResult(found=False)
    if status == 403:
        return PieceResult(found=False, not_ours=True)
    logger.warning("comms.usps_error", extra={"what": "piece", "status": status, "code": ""})
    if status == 429:
        retry = 60
        with contextlib.suppress(TypeError, ValueError):
            retry = int({k.lower(): v for k, v in headers.items()}.get("retry-after", 60))
        raise UspsRateLimited(max(1, min(retry, 600)))
    if status == 401:
        raise UspsAuthError(401, "auth")
    raise UspsError(status, "server" if status >= 500 else "unexpected")


def check_login() -> Config:
    """Sign in only (no piece query): for ``manage.py check_usps_iv``."""
    cfg = config()
    if not cfg.configured:
        raise UspsError(0, "not_configured")
    with _token.lock:
        forget_token()
        authenticate()
    return cfg

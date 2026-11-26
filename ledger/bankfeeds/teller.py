"""Teller (teller.io) provider, standard library only.

Teller's API needs mutual TLS with the certificate and private key from the
Teller dashboard (TELLER_CERTIFICATE_B64 / TELLER_PRIVATE_KEY_B64, base64 of
the PEM files) in the development and production environments; the sandbox
works without one. The access token from Teller Connect is sent as the HTTP
Basic user name.

Only https://api.teller.io is ever called, redirects are refused, every call
has a timeout, and failures are reported by HTTP status code alone: response
bodies and tokens never reach logs, messages or the database.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import json
import logging
import os
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from ledger.bankfeeds.base import (
    BANK_ID_MAX,
    KIND_MAX,
    AccountInfo,
    BalanceInfo,
    BankFeedError,
    Provider,
    TransactionInfo,
    clean_description,
    clean_text,
    last_four,
    to_cents,
)

logger = logging.getLogger(__name__)

API_HOST = "api.teller.io"
API_BASE = f"https://{API_HOST}"
# Teller publishes Connect only at this unversioned URL, with no integrity
# hash (checked 2026-11-26), so it cannot be pinned with SRI. The accepted
# residual risk and its limits are in D34-fixinfra-2 and docs/runbook.md.
CONNECT_SCRIPT = "https://cdn.teller.io/connect/connect.js"
# The connect page's Content-Security-Policy additions (that one view only).
# Scripts only from Teller's /connect/ path, not the whole CDN host.
CONNECT_CSP = {
    "script-src": ["https://cdn.teller.io/connect/"],
    "frame-src": ["https://cdn.teller.io", "https://api.teller.io"],
    "connect-src": ["https://cdn.teller.io", "https://api.teller.io"],
}
ENVIRONMENTS = ("sandbox", "development", "production")
TIMEOUT_SECONDS = 20
PAGE_SIZE = 250
MAX_PAGES = 40
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
REAUTH_STATUSES = {401, 403}


class TellerError(BankFeedError):
    def __init__(self, status: int, *, needs_reauth: bool = False):
        self.status = status
        self.needs_reauth = needs_reauth
        if needs_reauth:
            message = f"The bank asks to be reconnected (Teller answered {status})."
        elif status:
            message = f"Teller answered {status}; try again later."
        else:
            message = "Teller could not be reached; try again later."
        super().__init__(message)


def environment() -> str:
    env = (settings.TELLER_ENVIRONMENT or "sandbox").strip().lower()
    if env not in ENVIRONMENTS:
        raise BankFeedError("TELLER_ENVIRONMENT must be sandbox, development or production.")
    return env


def _pem(name: str) -> bytes:
    raw = getattr(settings, name, "") or ""
    if not raw:
        return b""
    try:
        data = base64.b64decode("".join(raw.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BankFeedError(f"{name} is not valid base64 (use base64 -w0).") from exc
    if b"-----BEGIN" not in data:
        raise BankFeedError(f"{name} is not a PEM file.")
    return data


def certificate_configured() -> bool:
    return bool(settings.TELLER_CERTIFICATE_B64 and settings.TELLER_PRIVATE_KEY_B64)


def configured() -> bool:
    """Enough to open Teller Connect and call the API in this environment."""
    try:
        env = environment()
    except BankFeedError:
        return False
    if not settings.TELLER_APPLICATION_ID:
        return False
    return env == "sandbox" or certificate_configured()


def ssl_context() -> ssl.SSLContext:
    """A verifying TLS context carrying the Teller client certificate.

    The standard library loads a certificate chain only from a file, so the
    PEMs are written to a private (0600) temporary file, loaded, and the file
    is removed straight away, whatever happens.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    cert, key = _pem("TELLER_CERTIFICATE_B64"), _pem("TELLER_PRIVATE_KEY_B64")
    if not (cert and key):
        if environment() != "sandbox":
            raise BankFeedError(
                "The Teller certificate and key are not set (TELLER_CERTIFICATE_B64, "
                "TELLER_PRIVATE_KEY_B64)."
            )
        return context
    fd, path = tempfile.mkstemp(prefix="teller-", suffix=".pem")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, cert.rstrip(b"\n") + b"\n" + key)
        os.close(fd)
        fd = -1
        try:
            context.load_cert_chain(path)
        except (ssl.SSLError, ValueError) as exc:
            raise BankFeedError("The Teller certificate and key do not load.") from exc
    finally:
        if fd != -1:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    return context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def build_opener(context: ssl.SSLContext):
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())


class TellerProvider(Provider):
    name = "teller"

    def __init__(self, access_token: str, *, opener=None):
        if not access_token:
            raise BankFeedError("This connection has no access token.")
        self._auth = "Basic " + base64.b64encode(f"{access_token}:".encode()).decode()
        self._opener = opener

    def __repr__(self) -> str:  # never show the token
        return "<TellerProvider>"

    def _get_opener(self):
        if self._opener is None:
            self._opener = build_opener(ssl_context())
        return self._opener

    def _call(self, path: str, params: dict | None = None, method: str = "GET"):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.port:
            raise BankFeedError("Only api.teller.io may be called.")
        request = urllib.request.Request(  # noqa: S310 - fixed https host checked above
            url, method=method, headers={"Authorization": self._auth, "Accept": "application/json"}
        )
        try:
            with self._get_opener().open(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            status = exc.code
            # The body may hold a reason code; read the code only, never keep or log text.
            code = ""
            with contextlib.suppress(Exception):
                code = str((json.loads(exc.read(4096) or b"{}").get("error") or {}).get("code"))
            with contextlib.suppress(Exception):
                exc.close()
            reauth = status in REAUTH_STATUSES or code.startswith("enrollment.disconnected")
            logger.warning("bank_feed.teller_error", extra={"status": status})
            raise TellerError(status, needs_reauth=reauth) from None
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
            logger.warning("bank_feed.teller_unreachable", extra={"status": 0})
            raise TellerError(0) from None
        if status == 204 or method == "DELETE":
            return None
        if len(body) > MAX_RESPONSE_BYTES:
            raise TellerError(status)
        try:
            return json.loads(body)
        except ValueError:
            raise TellerError(status) from None

    def list_accounts(self) -> list[AccountInfo]:
        data = self._call("/accounts")
        if not isinstance(data, list):
            raise TellerError(200)
        out = []
        for row in data:
            if not isinstance(row, dict) or row.get("status", "open") != "open":
                continue
            institution = row.get("institution") or {}
            out.append(
                AccountInfo(
                    external_id=clean_text(row.get("id"), 64),
                    name=clean_text(row.get("name"), 80) or "Bank account",
                    institution_name=clean_text(institution.get("name"), 80),
                    last_four=last_four(row.get("last_four")),
                    kind=clean_text(row.get("type"), 20),
                )
            )
        return [a for a in out if a.external_id]

    def transactions(self, account_id: str, since: datetime.date) -> list[TransactionInfo]:
        """Posted transactions on or after ``since``, following Teller's
        pagination (newest first; ``from_id`` = the last id of the page)."""
        path = f"/accounts/{urllib.parse.quote(account_id, safe='')}/transactions"
        out: list[TransactionInfo] = []
        from_id = None
        for _ in range(MAX_PAGES):
            params = {"count": PAGE_SIZE}
            if from_id:
                params["from_id"] = from_id
            page = self._call(path, params)
            if not isinstance(page, list):
                raise TellerError(200)
            oldest = None
            for row in page:
                if not isinstance(row, dict):
                    continue
                try:
                    posted = datetime.date.fromisoformat(str(row.get("date"))[:10])
                except ValueError:
                    continue
                oldest = posted if oldest is None else min(oldest, posted)
                if row.get("status") != "posted" or posted < since:
                    continue
                bank_id = clean_text(row.get("id"), BANK_ID_MAX)
                if not bank_id:
                    continue
                out.append(
                    TransactionInfo(
                        bank_id=bank_id,
                        posted_on=posted,
                        amount_cents=to_cents(row.get("amount")),
                        description=clean_description(row.get("description")),
                        kind=clean_text(row.get("type"), KIND_MAX),
                    )
                )
            if len(page) < PAGE_SIZE or (oldest is not None and oldest < since):
                break
            last = page[-1] if isinstance(page[-1], dict) else {}
            from_id = last.get("id")
            if not from_id:
                break
        return out

    def balance(self, account_id: str) -> BalanceInfo | None:
        from django.utils import timezone

        data = self._call(f"/accounts/{urllib.parse.quote(account_id, safe='')}/balances")
        if not isinstance(data, dict) or data.get("ledger") in (None, ""):
            return None
        return BalanceInfo(as_of=timezone.localdate(), balance_cents=to_cents(data["ledger"]))

    def remove_account(self, account_id: str) -> None:
        """Ask Teller to forget the account (best effort on disconnect)."""
        self._call(f"/accounts/{urllib.parse.quote(account_id, safe='')}", method="DELETE")

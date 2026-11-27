"""A small Stripe REST client on urllib (no SDK dependency, D32-payments).

* Only ``https://api.stripe.com``, with Python's default certificate
  verification (no custom CA, no verify=False, no proxy settings of ours).
* Redirects are refused (urllib would otherwise copy the Authorization
  header to whatever host a 30x names).
* 10 second timeouts. POSTs carry an Idempotency-Key and are retried (with
  the same key) on network errors and 5xx/429; GETs are not retried (the
  poller simply asks again later).
* Errors are reported by HTTP status and Stripe error code only. Request
  and response bodies, keys and customer data are never logged.
* The API version is pinned so request and response shapes do not change
  under us (``2026-08-26.dahlia`` has the Checkout card funding filter and
  still accepts ``payment_method_types``).

Tests replace ``transport`` with a fake; nothing in the test suite reaches
the network.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from payments.providers import secret_key

logger = logging.getLogger("payments")

API_BASE = "https://api.stripe.com"
API_VERSION = "2026-08-26.dahlia"
TIMEOUT_SECONDS = 10
POST_ATTEMPTS = 3
_ID = re.compile(r"^[a-z]{2,8}_[A-Za-z0-9_]{4,250}$")


class StripeError(RuntimeError):
    def __init__(self, status: int, code: str = "", *, decline_code: str = "", object_id: str = ""):
        super().__init__(f"Stripe request failed (HTTP {status}{', ' + code if code else ''})")
        self.status = status
        self.code = code
        # A declined off-session payment (HTTP 402): the issuer's reason and
        # the PaymentIntent Stripe created, so the portal can read it back.
        self.decline_code = decline_code
        self.object_id = object_id


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the key must never reach another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def build_opener():
    return urllib.request.build_opener(_NoRedirect())


# Replaced in tests with a fake opener.
_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        _opener = build_opener()
    return _opener


def _transport(method: str, url: str, data: bytes | None, headers: dict) -> tuple[int, bytes]:
    if not url.startswith(API_BASE + "/"):
        raise StripeError(0, "bad_host")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with _get_opener().open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return response.status, response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        body = b""
        with contextlib.suppress(Exception):
            body = exc.read(65536)
        return exc.code, body


# Replaced in tests.
transport = _transport


def check_id(value: str) -> str:
    """A Stripe object id, safe to put in a URL path."""
    if not isinstance(value, str) or not _ID.match(value):
        raise StripeError(0, "bad_id")
    return value


def _encode(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: a[b][0]=c."""
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out += _encode(value, name)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out += _encode(item, f"{name}[{i}]")
                else:
                    out.append((f"{name}[{i}]", _scalar(item)))
        elif value is not None:
            out.append((name, _scalar(value)))
    return out


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode(params: dict) -> bytes:
    return urllib.parse.urlencode(_encode(params)).encode()


def _headers(idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {secret_key()}",
        "Stripe-Version": API_VERSION,
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


# Answers that mean Stripe is unreachable or refuses the key, rather than
# a payment problem: counted per hour for the Payment processing check (D74).
TROUBLE_STATUSES = frozenset({0, 401, 403, 429})


def _note_trouble(status: int, code: str = "") -> None:
    if status in TROUBLE_STATUSES or status >= 500:
        from core.ops_timeline import bump

        bump("stripe", code or f"http_{status}")


def _result(status: int, body: bytes, what: str) -> dict:
    data: dict = {}
    with contextlib.suppress(ValueError):
        data = json.loads(body or b"{}")
    if 200 <= status < 300 and isinstance(data, dict):
        return data
    code = decline = object_id = ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        code = str(error.get("code") or error.get("type") or "")[:60]
        decline = str(error.get("decline_code") or "")[:60]
        pi = error.get("payment_intent")
        pi_id = pi.get("id") if isinstance(pi, dict) else ""
        object_id = pi_id if isinstance(pi_id, str) and _ID.match(pi_id) else ""
    logger.warning("payments.stripe_error", extra={"what": what, "status": status, "code": code})
    _note_trouble(status, code)
    raise StripeError(status, code, decline_code=decline, object_id=object_id)


def get(path: str, params: dict | None = None, *, what: str = "get") -> dict:
    query = ("?" + urllib.parse.urlencode(_encode(params))) if params else ""
    try:
        status, body = transport("GET", f"{API_BASE}{path}{query}", None, _headers())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("payments.stripe_error", extra={"what": what, "status": 0, "code": ""})
        _note_trouble(0, "network")
        raise StripeError(0, "network") from exc
    return _result(status, body, what)


def post(path: str, params: dict, *, idempotency_key: str, what: str = "post") -> dict:
    if not idempotency_key:
        raise ValueError("Every Stripe POST needs an Idempotency-Key.")
    data = encode(params)
    last: Exception | None = None
    for _attempt in range(POST_ATTEMPTS):
        try:
            status, body = transport("POST", f"{API_BASE}{path}", data, _headers(idempotency_key))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            continue
        if status >= 500 or status == 429:
            last = StripeError(status)
            continue
        return _result(status, body, what)
    logger.warning("payments.stripe_error", extra={"what": what, "status": 0, "code": "retries"})
    _note_trouble(0, "retries")
    raise StripeError(getattr(last, "status", 0), "retries")


# --- The calls the portal makes ------------------------------------------------------------
# Writes: Checkout Sessions (payment and setup mode), Customers, off-session
# PaymentIntents (automatic payments) and detaching a PaymentMethod. Never a
# refund: the restricted key has no refund rights.


def create_checkout_session(params: dict, *, idempotency_key: str) -> dict:
    return post(
        "/v1/checkout/sessions", params, idempotency_key=idempotency_key, what="checkout_create"
    )


def retrieve_checkout_session(session_id: str) -> dict:
    return get(
        f"/v1/checkout/sessions/{check_id(session_id)}",
        {"expand": ["payment_intent.latest_charge.balance_transaction"]},
        what="checkout_get",
    )


def retrieve_payment_intent(pi_id: str) -> dict:
    return get(
        f"/v1/payment_intents/{check_id(pi_id)}",
        {"expand": ["latest_charge.balance_transaction"]},
        what="payment_intent_get",
    )


def retrieve_charge(charge_id: str) -> dict:
    return get(
        f"/v1/charges/{check_id(charge_id)}",
        {"expand": ["balance_transaction"]},
        what="charge_get",
    )


def list_refunds(charge_id: str) -> dict:
    return get("/v1/refunds", {"charge": check_id(charge_id), "limit": 100}, what="refunds_list")


def list_disputes(charge_id: str) -> dict:
    return get("/v1/disputes", {"charge": check_id(charge_id), "limit": 100}, what="disputes_list")


def list_payouts(params: dict) -> dict:
    return get("/v1/payouts", params, what="payouts_list")


def list_balance_transactions(params: dict) -> dict:
    return get("/v1/balance_transactions", params, what="balance_transactions_list")


# --- Automatic payments (payments.autopay) -------------------------------------------------


def create_customer(params: dict, *, idempotency_key: str) -> dict:
    return post("/v1/customers", params, idempotency_key=idempotency_key, what="customer_create")


def retrieve_setup_session(session_id: str) -> dict:
    return get(
        f"/v1/checkout/sessions/{check_id(session_id)}",
        {"expand": ["setup_intent.payment_method"]},
        what="setup_session_get",
    )


def retrieve_setup_intent(seti_id: str) -> dict:
    return get(
        f"/v1/setup_intents/{check_id(seti_id)}",
        {"expand": ["payment_method"]},
        what="setup_intent_get",
    )


def create_payment_intent(params: dict, *, idempotency_key: str) -> dict:
    return post(
        "/v1/payment_intents",
        params,
        idempotency_key=idempotency_key,
        what="payment_intent_create",
    )


def list_payment_intents(params: dict) -> dict:
    return get("/v1/payment_intents", params, what="payment_intents_list")


def detach_payment_method(pm_id: str) -> dict:
    return post(
        f"/v1/payment_methods/{check_id(pm_id)}/detach",
        {},
        idempotency_key=f"detach-{pm_id}",
        what="payment_method_detach",
    )

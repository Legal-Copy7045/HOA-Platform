"""An in-memory stand-in for api.stripe.com (no network in tests)."""

from __future__ import annotations

import itertools
import json
import time
import urllib.parse


def _today_midnight_utc() -> int:
    import datetime

    from django.utils import timezone

    day = timezone.localdate()
    return int(datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC).timestamp())


def _unflatten(pairs) -> dict:
    """a[b][0]=c form encoding back into nested dicts/lists."""
    out: dict = {}
    for key, value in pairs:
        parts = key.replace("]", "").split("[")
        node = out
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, {})
    return _listify(out)


def _listify(node):
    if isinstance(node, dict):
        if node and all(k.isdigit() for k in node):
            return [_listify(node[k]) for k in sorted(node, key=int)]
        return {k: _listify(v) for k, v in node.items()}
    return node


class FakeStripe:
    def __init__(self):
        self.calls: list[dict] = []
        self.sessions: dict[str, dict] = {}
        self.intents: dict[str, dict] = {}
        self.charges: dict[str, dict] = {}
        self.refunds: dict[str, list] = {}
        self.disputes: dict[str, list] = {}
        self.balance: list[dict] = []
        self.payouts: list[dict] = []
        self.payout_items: dict[str, list] = {}
        self.fail_next: tuple[int, dict] | None = None
        self.livemode = False
        # Automatic payments: customers, saved payment methods, setup intents,
        # and what the next off-session charge on a payment method does
        # ("succeeded", "processing", "decline:<code>", "authentication_required").
        self.customers: dict[str, dict] = {}
        self.payment_methods: dict[str, dict] = {}
        self.setup_intents: dict[str, dict] = {}
        self.outcomes: dict[str, list[str]] = {}
        self.idempotent: dict[tuple[str, str], tuple[int, dict]] = {}
        self.drop_responses = 0  # answer the next N POSTs with 500 after doing them
        self._ids = itertools.count(1000)

    def _id(self, prefix: str) -> str:
        return f"{prefix}_test{next(self._ids)}abcd"

    # transport(method, url, data, headers) -> (status, body)
    def __call__(self, method, url, data, headers):
        parsed = urllib.parse.urlsplit(url)
        params = _unflatten(urllib.parse.parse_qsl(parsed.query))
        body = _unflatten(urllib.parse.parse_qsl(data.decode())) if data else {}
        self.calls.append(
            {
                "method": method,
                "path": parsed.path,
                "params": params,
                "body": body,
                "headers": headers,
            }
        )
        if self.fail_next is not None:
            status, payload = self.fail_next
            self.fail_next = None
            return status, json.dumps(payload).encode()
        key = (parsed.path, headers.get("Idempotency-Key", ""))
        if method == "POST" and key in self.idempotent:
            status, payload = self.idempotent[key]  # Stripe replays the first answer
        else:
            status, payload = self._route(method, parsed.path, params, body)
            if method == "POST" and key[1] and self._replayable(parsed.path):
                self.idempotent[key] = (status, payload)
        if method == "POST" and self.drop_responses:
            self.drop_responses -= 1
            return 500, b'{"error": {"type": "api_error"}}'
        return status, json.dumps(payload).encode()

    @staticmethod
    def _replayable(path: str) -> bool:
        return path.startswith(("/v1/customers", "/v1/payment_intents", "/v1/payment_methods"))

    def _route(self, method, path, params, body):
        parts = path.strip("/").split("/")
        if method == "POST" and path == "/v1/checkout/sessions":
            return 200, self._create_session(body)
        if method == "GET" and parts[:3] == ["v1", "checkout", "sessions"] and len(parts) == 4:
            s = self.sessions.get(parts[3])
            if not s:
                return 404, {"error": {"code": "resource_missing"}}
            out = dict(s)
            pi = self.intents.get(s.get("payment_intent") or "")
            out["payment_intent"] = self._pi(pi) if pi else None
            seti = self.setup_intents.get(s.get("setup_intent") or "")
            out["setup_intent"] = self._seti(seti) if seti else None
            return 200, out
        if method == "POST" and path == "/v1/customers":
            cid = self._id("cus")
            self.customers[cid] = {
                "id": cid,
                "object": "customer",
                "livemode": self.livemode,
                "metadata": body.get("metadata", {}),
                "_body": body,
            }
            return 200, self.customers[cid]
        if method == "GET" and parts[:2] == ["v1", "setup_intents"] and len(parts) == 3:
            seti = self.setup_intents.get(parts[2])
            if not seti:
                return 404, {"error": {"code": "resource_missing"}}
            return 200, self._seti(seti)
        if method == "POST" and path == "/v1/payment_intents":
            return self._create_intent(body)
        if method == "GET" and path == "/v1/payment_intents":
            since = int((params.get("created") or {}).get("gte") or 0)
            data = [
                self._pi(pi)
                for pi in self.intents.values()
                if pi.get("customer") == params.get("customer") and pi.get("created", 0) >= since
            ]
            return 200, {"data": data, "has_more": False}
        if method == "POST" and parts[:2] == ["v1", "payment_methods"] and parts[-1] == "detach":
            pm = self.payment_methods.get(parts[2])
            if not pm or not pm.get("customer"):
                return 404, {"error": {"code": "resource_missing"}}
            pm["customer"] = None
            return 200, pm
        if method == "GET" and parts[:2] == ["v1", "payment_intents"]:
            pi = self.intents.get(parts[2])
            return (200, self._pi(pi)) if pi else (404, {"error": {"code": "resource_missing"}})
        if method == "GET" and parts[:2] == ["v1", "charges"]:
            ch = self.charges.get(parts[2])
            return (200, ch) if ch else (404, {"error": {"code": "resource_missing"}})
        if method == "GET" and path == "/v1/refunds":
            return 200, {"data": self.refunds.get(params.get("charge"), []), "has_more": False}
        if method == "GET" and path == "/v1/disputes":
            return 200, {"data": self.disputes.get(params.get("charge"), []), "has_more": False}
        if method == "GET" and path == "/v1/balance_transactions":
            if "payout" in params:
                return 200, {"data": self.payout_items.get(params["payout"], []), "has_more": False}
            return 200, {"data": self.balance, "has_more": False}
        if method == "GET" and path == "/v1/payouts":
            return 200, {"data": self.payouts, "has_more": False}
        return 404, {"error": {"code": "resource_missing"}}

    def _pi(self, pi):
        out = dict(pi)
        ch = self.charges.get(pi.get("latest_charge") or "")
        out["latest_charge"] = ch
        return out

    def _create_session(self, body):
        sid = self._id("cs")
        total = sum(int(li["price_data"]["unit_amount"]) for li in body.get("line_items", []))
        self.sessions[sid] = {
            "id": sid,
            "object": "checkout.session",
            "url": f"https://checkout.stripe.com/c/pay/{sid}",
            "client_reference_id": body.get("client_reference_id"),
            "metadata": body.get("metadata", {}),
            "livemode": self.livemode,
            "currency": "usd",
            "amount_total": total,
            "status": "open",
            "payment_status": "unpaid",
            "payment_intent": None,
            "mode": body.get("mode", "payment"),
            "customer": body.get("customer"),
            "setup_intent": None,
            "_pi_metadata": body.get("payment_intent_data", {}).get("metadata", {}),
            "_body": body,
        }
        return self.sessions[sid]

    # --- what a payer does on Stripe's page -----------------------------------------------

    def pay(self, session_id, *, funding="credit", bank=False, status="succeeded", fee=None):
        s = self.sessions[session_id]
        pi_id, ch_id, bt_id = self._id("pi"), self._id("py" if bank else "ch"), self._id("txn")
        amount = s["amount_total"]
        fee = fee if fee is not None else (amount * 29 // 1000 + 30)
        details = (
            {"type": "us_bank_account", "us_bank_account": {"last4": "6789"}}
            if bank
            else {"type": "card", "card": {"funding": funding, "brand": "visa", "last4": "4242"}}
        )
        succeeded = status == "succeeded"
        self.charges[ch_id] = {
            "id": ch_id,
            "amount": amount,
            "currency": "usd",
            "captured": succeeded,
            "paid": succeeded,
            "status": "succeeded" if succeeded else "pending",
            "livemode": self.livemode,
            "payment_method_details": details,
            "balance_transaction": {"id": bt_id, "fee": fee, "net": amount - fee, "amount": amount}
            if succeeded
            else None,
        }
        self.intents[pi_id] = {
            "id": pi_id,
            "amount": amount,
            "amount_received": amount if succeeded else 0,
            "currency": "usd",
            "livemode": self.livemode,
            "status": status,
            "metadata": s["_pi_metadata"],
            "latest_charge": ch_id,
        }
        s.update(
            status="complete",
            payment_status="paid" if succeeded else "unpaid",
            payment_intent=pi_id,
        )
        if succeeded:
            self.balance.append(
                {
                    "id": bt_id,
                    "type": "payment" if bank else "charge",
                    "source": ch_id,
                    "amount": amount,
                    "fee": fee,
                    "net": amount - fee,
                    "created": int(time.time()) - 3 * 86400,
                    "livemode": self.livemode,
                }
            )
        return pi_id, ch_id

    def settle_ach(self, pi_id, *, ok=True, fee=80, code="insufficient_funds"):
        pi = self.intents[pi_id]
        ch = self.charges[pi["latest_charge"]]
        if ok:
            bt_id = self._id("txn")
            pi.update(status="succeeded", amount_received=pi["amount"])
            ch.update(
                captured=True,
                paid=True,
                status="succeeded",
                balance_transaction={"id": bt_id, "fee": fee},
            )
            self.balance.append(
                {
                    "id": bt_id,
                    "type": "payment",
                    "source": ch["id"],
                    "amount": pi["amount"],
                    "fee": fee,
                    "net": pi["amount"] - fee,
                    "created": int(time.time()) - 3 * 86400,
                    "livemode": self.livemode,
                }
            )
        else:
            pi.update(status="requires_payment_method", last_payment_error={"code": code})
            ch.update(status="failed")

    def payout(self, items, *, amount=None):
        po_id = self._id("po")
        net = sum(b["net"] for b in items)
        self.payouts.append(
            {
                "id": po_id,
                "amount": net if amount is None else amount,
                "status": "paid",
                # Like Stripe: the arrival day as midnight UTC of that date
                # (the site's local "today"), not the current instant, so
                # tests don't depend on the hour they run at.
                "arrival_date": _today_midnight_utc(),
                "livemode": self.livemode,
            }
        )
        self.payout_items[po_id] = list(items)
        return po_id

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]

    # --- automatic payments ------------------------------------------------------------------

    def _seti(self, seti):
        out = dict(seti)
        out["payment_method"] = self.payment_methods.get(seti.get("payment_method") or "")
        return out

    def complete_setup(
        self,
        session_id,
        *,
        funding="credit",
        brand="visa",
        last4="4242",
        bank_name="STRIPE TEST BANK",
        status="succeeded",
    ):
        """The payer saves a card or bank account on Stripe's setup page."""
        s = self.sessions[session_id]
        body = s["_body"]
        kind = body["payment_method_types"][0]
        pm_id, seti_id = self._id("pm"), self._id("seti")
        details = (
            {"bank_name": bank_name, "last4": last4, "account_type": "checking"}
            if kind == "us_bank_account"
            else {"brand": brand, "last4": last4, "funding": funding, "exp_year": 2030}
        )
        self.payment_methods[pm_id] = {
            "id": pm_id,
            "object": "payment_method",
            "type": kind,
            "customer": body["customer"],
            "livemode": self.livemode,
            kind: details,
        }
        self.setup_intents[seti_id] = {
            "id": seti_id,
            "object": "setup_intent",
            "status": status,
            "customer": body["customer"],
            "payment_method": pm_id,
            "mandate": self._id("mandate") if kind == "us_bank_account" else None,
            "metadata": body.get("setup_intent_data", {}).get("metadata", {}),
            "livemode": self.livemode,
        }
        s.update(status="complete", setup_intent=seti_id)
        return seti_id, pm_id

    def _create_intent(self, body):
        pm_id = body.get("payment_method") or ""
        pm = self.payment_methods.get(pm_id)
        if not pm or pm.get("customer") != body.get("customer"):
            return 400, {"error": {"type": "invalid_request_error", "code": "resource_missing"}}
        queue = self.outcomes.get(pm_id) or []
        outcome = queue.pop(0) if queue else "succeeded"
        bank = pm["type"] == "us_bank_account"
        amount = int(body["amount"])
        pi_id, ch_id = self._id("pi"), self._id("py" if bank else "ch")
        details = (
            {"type": "us_bank_account", "us_bank_account": {"last4": pm[pm["type"]]["last4"]}}
            if bank
            else {"type": "card", "card": dict(pm["card"])}
        )
        succeeded = outcome == "succeeded"
        pending = outcome == "processing"
        bt_id = self._id("txn") if succeeded else None
        fee = amount * 29 // 1000 + 30
        self.charges[ch_id] = {
            "id": ch_id,
            "amount": amount,
            "currency": "usd",
            "captured": succeeded,
            "paid": succeeded,
            "status": "succeeded" if succeeded else ("pending" if pending else "failed"),
            "livemode": self.livemode,
            "payment_method_details": details,
            "balance_transaction": (
                {"id": bt_id, "fee": fee, "net": amount - fee, "amount": amount}
                if succeeded
                else None
            ),
        }
        if succeeded:
            status = "succeeded"
        elif pending:
            status = "processing"
        else:
            status = "requires_payment_method"
        pi = {
            "id": pi_id,
            "object": "payment_intent",
            "amount": amount,
            "amount_received": amount if succeeded else 0,
            "currency": "usd",
            "livemode": self.livemode,
            "status": status,
            "metadata": body.get("metadata", {}),
            "customer": body.get("customer"),
            "payment_method": pm_id,
            "latest_charge": ch_id,
            "created": int(time.time()),
            "last_payment_error": None,
            "_body": body,
        }
        self.intents[pi_id] = pi
        if succeeded:
            self.balance.append(
                {
                    "id": bt_id,
                    "type": "payment" if bank else "charge",
                    "source": ch_id,
                    "amount": amount,
                    "fee": fee,
                    "net": amount - fee,
                    "created": int(time.time()) - 3 * 86400,
                    "livemode": self.livemode,
                }
            )
        if succeeded or pending:
            return 200, {k: v for k, v in pi.items() if k != "_body"}
        if outcome == "authentication_required":
            error = {"type": "card_error", "code": "authentication_required"}
        else:
            error = {
                "type": "card_error",
                "code": "card_declined",
                "decline_code": outcome.partition(":")[2] or "generic_decline",
            }
        pi["last_payment_error"] = dict(error)
        return 402, {"error": {**error, "payment_intent": self._pi(pi)}}

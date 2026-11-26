"""An in-memory stand-in for api.stripe.com (no network in tests)."""

from __future__ import annotations

import itertools
import json
import time
import urllib.parse


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
        status, payload = self._route(method, parsed.path, params, body)
        return status, json.dumps(payload).encode()

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
            return 200, out
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

    def settle_ach(self, pi_id, *, ok=True, fee=80):
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
            pi.update(status="requires_payment_method")
            ch.update(status="failed")

    def payout(self, items, *, amount=None):
        po_id = self._id("po")
        net = sum(b["net"] for b in items)
        self.payouts.append(
            {
                "id": po_id,
                "amount": net if amount is None else amount,
                "status": "paid",
                "arrival_date": int(time.time()),
                "livemode": self.livemode,
            }
        )
        self.payout_items[po_id] = list(items)
        return po_id

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]

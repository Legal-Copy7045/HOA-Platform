"""Everything outside the portal, faked (no network, ever).

* Stripe: ``payments/tests/fake_stripe.py`` (the suite's own fake), with the
  balance lines dated when they happen and weekly payouts to the fake bank.
* The bank (Teller): ``FakeBank`` is the association's bank. It holds an
  operating and a reserve account, posts lines for what really moves money
  (deposits the manager takes to the bank, Stripe payouts, vendor checks the
  payee cashes, transfers, fees and interest) a day after they happen, and
  answers the portal's Teller provider calls (accounts, transactions since a
  date, the balance at the end of yesterday).
* Email: ``FakeSmtp2go`` answers SMTP2Go's send and activity API; every
  accepted message is delivered except to the addresses the scenario marked
  as bouncing (hard bounce), so the portal's delivery reports, bounce records
  and paper-copy items run as in production.
* USPS: ``comms/tests/fake_usps.py`` with every piece "Delivered" 3 days on.
* AI (signed ARC forms): a reader that returns the expected signer.
"""

from __future__ import annotations

import datetime
import hashlib
import itertools
import json
import time
from dataclasses import dataclass, field

from django.utils import timezone

from payments.tests.fake_stripe import FakeStripe


class SimStripe(FakeStripe):
    """The suite's fake, with balance lines dated now (the fake back-dates them
    three days for its own tests) and the payout of everything not paid out."""

    def __init__(self):
        super().__init__()
        self.paid_out: set[str] = set()

    def _fix_dates(self):
        now = int(time.time())
        for b in self.balance:
            if b["id"] not in self.paid_out and b.get("_dated") is None:
                b["created"] = now
                b["_dated"] = True

    def __call__(self, method, url, data, headers):
        out = super().__call__(method, url, data, headers)
        self._fix_dates()
        return out

    def pay(self, *args, **kwargs):
        out = super().pay(*args, **kwargs)
        self._fix_dates()
        return out

    def settle_ach(self, *args, **kwargs):
        out = super().settle_ach(*args, **kwargs)
        self._fix_dates()
        return out

    def payout_pending(self):
        """Pay out every balance line not yet paid out; returns (payout id, net)."""
        items = [b for b in self.balance if b["id"] not in self.paid_out]
        if not items:
            return None, 0
        clean = [{k: v for k, v in b.items() if not k.startswith("_")} for b in items]
        po_id = self.payout(clean)
        for b in items:
            self.paid_out.add(b["id"])
        return po_id, sum(b["net"] for b in items)


# --- The bank --------------------------------------------------------------------------------


@dataclass
class BankLine:
    bank_id: str
    posted_on: datetime.date
    amount_cents: int
    description: str
    kind: str = ""


@dataclass
class BankAccount:
    external_id: str
    name: str
    last_four: str
    opening_cents: int
    lines: list[BankLine] = field(default_factory=list)

    def balance_through(self, day: datetime.date) -> int:
        return self.opening_cents + sum(x.amount_cents for x in self.lines if x.posted_on <= day)


class FakeBank:
    """The association's bank, as Teller would show it."""

    INSTITUTION = "Example Community Bank (fictional)"

    def __init__(self, opening_operating: int, opening_reserve: int):
        self.accounts = {
            "acc_sim_operating": BankAccount(
                "acc_sim_operating", "Operating checking (sim)", "0101", opening_operating
            ),
            "acc_sim_reserve": BankAccount(
                "acc_sim_reserve", "Reserve savings (sim)", "0202", opening_reserve
            ),
        }
        self._ids = itertools.count(1)
        self.calls = 0

    @property
    def operating(self) -> BankAccount:
        return self.accounts["acc_sim_operating"]

    @property
    def reserve(self) -> BankAccount:
        return self.accounts["acc_sim_reserve"]

    def post(self, account: BankAccount, on: datetime.date, cents: int, text: str, kind=""):
        line = BankLine(f"sim-{next(self._ids):06d}", on, cents, text, kind)
        account.lines.append(line)
        return line

    # The portal's provider interface (ledger.bankfeeds.base.Provider).
    def provider(self):
        from ledger.bankfeeds.base import AccountInfo, BalanceInfo, Provider, TransactionInfo

        bank = self

        class _Teller(Provider):
            name = "teller"

            def list_accounts(self):
                bank.calls += 1
                return [
                    AccountInfo(a.external_id, a.name, bank.INSTITUTION, a.last_four, "depository")
                    for a in bank.accounts.values()
                ]

            def transactions(self, account_id, since):
                bank.calls += 1
                yesterday = timezone.localdate() - datetime.timedelta(days=1)
                acct = bank.accounts[account_id]
                return [
                    TransactionInfo(x.bank_id, x.posted_on, x.amount_cents, x.description, x.kind)
                    for x in acct.lines
                    if since <= x.posted_on <= yesterday
                ]

            def balance(self, account_id):
                yesterday = timezone.localdate() - datetime.timedelta(days=1)
                acct = bank.accounts[account_id]
                return BalanceInfo(yesterday, acct.balance_through(yesterday))

        return _Teller()


# --- Email (SMTP2Go) -------------------------------------------------------------------------


class FakeSmtp2go:
    """SMTP2Go's v3 API: send, activity search, stats. Delivered unless bouncing."""

    def __init__(self):
        self.sent: list[dict] = []
        self.events: list[dict] = []
        self.bouncing: set[str] = set()
        self._ids = itertools.count(1)
        self.searches = 0

    def __call__(self, url, data, headers, timeout):
        body = json.loads(data)
        path = url.split("/v3/", 1)[1]
        if path == "email/send":
            email_id = f"sim{next(self._ids):07d}-zz"
            at = timezone.now()
            self.sent.append(
                {
                    "email_id": email_id,
                    "to": list(body.get("to", [])),
                    "subject": body.get("subject", ""),
                    "at": at,
                }
            )
            for rcpt in body.get("to", []):
                address = rcpt.rpartition("<")[2].rstrip(">").strip().lower()
                bounced = address in self.bouncing
                self.events.append(
                    {
                        "email_id": email_id,
                        "event": "hard_bounce" if bounced else "delivered",
                        "recipient": address,
                        "date": (at + datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "_at": at + datetime.timedelta(minutes=2),
                    }
                )
            return 200, json.dumps(
                {"data": {"succeeded": len(body["to"]), "failed": 0, "email_id": email_id}}
            ).encode()
        if path == "activity/search":
            self.searches += 1
            start = datetime.datetime.fromisoformat(body["start_date"].replace("Z", "+00:00"))
            end = datetime.datetime.fromisoformat(body["end_date"].replace("Z", "+00:00"))
            rows = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in self.events
                if start <= e["_at"] <= end
            ]
            limit = int(body.get("limit") or 500)
            offset = int(body.get("continue_token") or 0)
            page = rows[offset : offset + limit]
            token = str(offset + limit) if offset + limit < len(rows) else ""
            return 200, json.dumps({"data": {"events": page, "continue_token": token}}).encode()
        if path == "stats/email_summary":
            return 200, json.dumps({"data": {"cycle_used": len(self.sent)}}).encode()
        return 404, b"{}"

    def prune(self, before: datetime.datetime) -> None:
        """Forget activity older than the portal ever reads back (6 hours)."""
        self.events = [e for e in self.events if e["_at"] >= before]


# --- AI --------------------------------------------------------------------------------------


def fake_ai_reader(data: bytes, content_type: str, expected: list[dict]) -> list[dict]:
    """Reads a "signed" ARC form: every expected neighbor approved (fictional)."""
    out = []
    for item in expected:
        out.append({**item, "position": "approve", "signer_name": item.get("name", "")})
    return out


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# --- Installing them -------------------------------------------------------------------------


class Fakes:
    def __init__(self, *, opening_operating: int = 2_500_000, opening_reserve: int = 4_000_000):
        self.stripe = SimStripe()
        self.bank = FakeBank(opening_operating, opening_reserve)
        self.smtp2go = FakeSmtp2go()
        self._undo: list[tuple[object, str, object]] = []

    def _patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def install(self) -> None:
        from comms import smtp2go
        from core import ai
        from ledger.bankfeeds import services as bank_services
        from payments import stripe_api

        self._patch(stripe_api, "transport", self.stripe)
        self._patch(smtp2go, "transport", self.smtp2go)
        provider = self.bank.provider()
        self._patch(bank_services, "for_connection", lambda conn, **kw: provider)
        if hasattr(bank_services, "TellerProvider"):
            self._patch(bank_services, "TellerProvider", lambda token, **kw: provider)
        from documents import scanning
        from documents.models import ScanResult

        self._patch(scanning, "_clamd", lambda fileobj: (ScanResult.CLEAN, ""))
        self._patch(ai, "read_signed_form", fake_ai_reader)
        self._patch(ai, "api_key", lambda: "sim-fake-ai")

    def uninstall(self) -> None:
        while self._undo:
            module, name, value = self._undo.pop()
            setattr(module, name, value)

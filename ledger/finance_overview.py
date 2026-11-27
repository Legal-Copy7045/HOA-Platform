"""Finance overview (D89): one card per finance component the person may
open, with its feature on, grouped (money in, money out, books, planning),
each with a one-line status, its own waiting count and a link.

The counts are the same My items counts that make up the menu bubbles
(core.my_items.nav_counts), so the overview's bubble always equals the sum
of its cards' numbers. Figures come from cheap existing queries, one or two
per card at most, never one per home. Read-only: nothing here changes a
record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import reverse
from django.utils import timezone

from core import features, policy


def _n(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


# --- Figures: one line each, from existing models and services -----------------------


def _owner_accounts(ctx) -> str:
    from core.templatetags.portal import cents

    bal = ctx.balances()
    owed = sum(v for v in bal.values() if v > 0)
    behind = sum(1 for v in bal.values() if v > 0)
    if not owed:
        return "Every home is paid up."
    return f"{cents(owed)} owed by {_n(behind, 'home')}."


def _collections(ctx) -> str:
    from ledger.models import CollectionCase

    n = CollectionCase.objects.for_user(ctx.user).exclude(state="closed").count()
    return f"{_n(n, 'open case')}." if n else "No open collection cases."


def _invoices(ctx) -> str:
    from operations.models import Invoice

    waiting = Invoice.objects.filter(state="pending_approval").count()
    unpaid = Invoice.objects.filter(state="approved").count()
    return f"{waiting} awaiting approval · {unpaid} approved, not yet paid."


def _online_payments(ctx) -> str:
    from payments.services import online_payments_enabled

    if online_payments_enabled(ctx.request):
        return "Owners can pay online."
    return "Not taking payments yet: the Stripe key is set on the server."


def _disbursements(ctx) -> str:
    from ledger.models import DisbursementBatch

    n = DisbursementBatch.objects.count()
    return f"{_n(n, 'disbursement')} recorded." if n else "No disbursements yet."


def _resales(ctx) -> str:
    from ledger.models import ResaleRequest

    n = (
        ResaleRequest.objects.for_user(ctx.user)
        .filter(state__in=["submitted", "in_progress"])
        .count()
    )
    return f"{_n(n, 'certificate')} being prepared." if n else "No certificates in progress."


def _accounting(ctx) -> str:
    from ledger.models import BankReconciliation

    last = BankReconciliation.objects.order_by("-statement_date", "-id").first()
    if last is None:
        return "No bank reconciliation recorded yet."
    return f"Last reconciled to the statement of {last.statement_date:%b %-d, %Y}."


def _bank_feed(ctx) -> str:
    from ledger.models import BankConnection

    n = BankConnection.objects.filter(status="active").count()
    return f"{_n(n, 'bank account')} connected." if n else "No bank account connected."


def _budgets(ctx) -> str:
    from ledger.models import Budget

    budget = Budget.objects.order_by("-fiscal_year").first()
    if budget is None:
        return "No budget yet."
    return f"FY{budget.fiscal_year}: {budget.get_status_display().lower()}."


def _reserve_plan(ctx) -> str:
    from ledger.models import ReserveStudySnapshot

    snap = ReserveStudySnapshot.objects.order_by("-adopted_on", "-id").first()
    if snap is None:
        return "No plan adopted yet."
    funded = "" if snap.percent_funded is None else f", {snap.percent_funded}% funded"
    return f"Adopted {snap.adopted_on:%b %-d, %Y}{funded}."


def _reports(ctx) -> str:
    from reports.registry import REPORTS

    return f"{_n(len(REPORTS), 'report')}, each as a page, CSV or PDF."


@dataclass(frozen=True)
class Card:
    group: str
    label: str  # the page's <h1>
    url_name: str
    icon: str
    permission: str
    figure: object  # callable(ctx) -> str
    # My items sidebar keys whose items wait on this page.
    counts: tuple[str, ...] = field(default=())


MONEY_IN, MONEY_OUT, BOOKS, PLANNING = "Money in", "Money out", "Books", "Planning"
GROUPS = (MONEY_IN, MONEY_OUT, BOOKS, PLANNING)

# Every finance component. A later component (books, tax...) adds a card here.
CARDS: tuple[Card, ...] = (
    Card(
        MONEY_IN,
        "Owner accounts",
        "ledger_home",
        "wallet",
        "finance.view",
        _owner_accounts,
        ("ledger_home",),
    ),
    Card(
        MONEY_IN,
        "Collections",
        "collections",
        "dollar",
        "finance.view",
        _collections,
        ("collections",),
    ),
    Card(
        MONEY_IN,
        "Online payments",
        "online_payments",
        "wallet",
        "payments.manage",
        _online_payments,
        ("online_payments",),
    ),
    Card(
        MONEY_IN,
        "Resale certificates",
        "resales",
        "file",
        "resale.manage",
        _resales,
        ("resales",),
    ),
    Card(
        MONEY_OUT,
        "Invoices",
        "invoices",
        "receipt",
        "finance.view",
        _invoices,
        ("invoices",),
    ),
    Card(
        MONEY_OUT,
        "Disbursements",
        "disbursements",
        "dollar",
        "disbursement.view",
        _disbursements,
        ("disbursements",),
    ),
    Card(
        BOOKS,
        "Accounting",
        "accounting",
        "receipt",
        "finance.view",
        _accounting,
        ("accounting",),
    ),
    Card(
        BOOKS,
        "Bank feed",
        "bank_feed",
        "activity",
        "bank_feed.view",
        _bank_feed,
        ("bank_feed",),
    ),
    Card(
        BOOKS,
        "Reports",
        "reports",
        "chart",
        "reports.view",
        _reports,
        ("reports",),
    ),
    Card(
        PLANNING,
        "Budgets",
        "budgets",
        "chart",
        "finance.view",
        _budgets,
        ("budgets",),
    ),
    Card(
        PLANNING,
        "Reserve plan",
        "reserve_plan",
        "chart",
        "reserves.view",
        _reserve_plan,
        ("reserve_plan",),
    ),
)
URL_NAMES = tuple(c.url_name for c in CARDS)


def open_cards(groups: frozenset[str], request=None) -> list[Card]:
    """The cards this person may open, with their features on."""
    return [
        c
        for c in CARDS
        if policy.PERMISSIONS[c.permission] & groups
        and features.url_name_enabled(c.url_name, request)
    ]


def card_count(card: Card, counts: dict[str, int]) -> int:
    return sum(counts.get(name, 0) for name in card.counts)


class _Context:
    def __init__(self, request):
        self.request = request
        self.user = request.user
        self._balances = None

    def balances(self):
        if self._balances is None:
            from ledger.services import balances

            self._balances = balances()
        return self._balances


def build(request) -> dict:
    """The overview page's data: summary tiles and the grouped cards."""
    from core.my_items import nav_counts
    from core.templatetags.portal import cents

    groups = policy.roles_for(request.user).groups
    counts = nav_counts(request.user, request)
    ctx = _Context(request)
    cards = open_cards(groups, request)
    rows = []
    for card in cards:
        rows.append(
            {
                "group": card.group,
                "label": card.label,
                "url": reverse(card.url_name),
                "icon": card.icon,
                "status": card.figure(ctx),
                "count": card_count(card, counts),
            }
        )
    tiles = []
    if policy.can(request.user, "finance.view"):
        bal = ctx.balances()
        tiles = [
            {
                "label": "Owed by owners",
                "value": cents(sum(v for v in bal.values() if v > 0)),
                "url": reverse("ledger_home"),
            },
            {
                "label": "Prepayment credits",
                "value": cents(-sum(v for v in bal.values() if v < 0)),
                "url": reverse("ledger_home"),
            },
        ]
    return {
        "groups": [
            {"title": g, "cards": [r for r in rows if r["group"] == g]}
            for g in GROUPS
            if any(r["group"] == g for r in rows)
        ],
        "tiles": tiles,
        "waiting": sum(r["count"] for r in rows),
        "today": timezone.localdate(),
    }

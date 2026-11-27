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

import datetime
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


def _bank_counts() -> tuple[int, int]:
    """(bank feed connections, bank accounts set up in the books): one bank
    status for every card, so the feed and the books never contradict each
    other on the same page (WT-004)."""
    from ledger.books.models import BankAccountSetup
    from ledger.models import BankConnection

    return (
        BankConnection.objects.filter(status="active").count(),
        BankAccountSetup.objects.count(),
    )


def _bank_feed(ctx) -> str:
    feed, books = _bank_counts()
    if not feed:
        return "No bank account connected."
    if not books:
        return (
            f"{_n(feed, 'bank account')} connected, not yet set up in the books: "
            "add it on Bank accounts and funds."
        )
    return f"{_n(feed, 'bank account')} connected; {_n(books, 'bank account')} in the books."


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


def _month(key: str) -> str:
    year, month = key.split("-")[:2]
    return datetime.date(int(year), int(month), 1).strftime("%B %Y")


def _books_close(ctx) -> str:
    from ledger.books import close, periods

    if not close.books_in_use():
        return "The books are not set up yet: add the bank account on Bank accounts and funds."
    last = periods.last_closed_month()
    closed = f"Closed through {_month(last)}" if last else "No month closed yet"
    exceptions = sum(r.blocking for r in close.open_runs())
    return f"{closed} · {_n(exceptions, 'open exception')}."


def _financial_statements(ctx) -> str:
    from ledger.books import close, periods

    last = periods.last_closed_month() if close.books_in_use() else None
    if last:
        return f"Final figures through {_month(last)}; later months are marked not closed."
    return "Balance sheet by fund, income and expenses, budget against actual."


def _books_accounts(ctx) -> str:
    feed, books = _bank_counts()
    if books:
        return f"{_n(books, 'bank account')} in the books."
    if feed:
        return (
            f"The bank feed has {_n(feed, 'account')} connected; set it up here so the books "
            "and the close use it."
        )
    return "No bank account in the books yet."


def _books_coding(ctx) -> str:
    from ledger.books import coding

    n = len(coding.active_rules())
    return f"{_n(n, 'rule')} coding bank lines by themselves." if n else "No coding rules yet."


def _year_end(ctx) -> str:
    from ledger.books.models import YearEndPackage

    pkg = YearEndPackage.objects.order_by("-fiscal_year", "-id").first()
    if pkg is None:
        return "No year-end package built yet."
    return f"Latest: fiscal year {pkg.fiscal_year}{' (year closed)' if pkg.year_closed else ''}."


def _tax(ctx) -> str:
    from ledger.books import periods, tax

    year = periods.fiscal_year_of(ctx.today) - 1
    return f"Form 1120-H for fiscal year {year} due {tax.filing_due(year):%b %-d, %Y}."


def _special_assessments(ctx) -> str:
    from ledger.models import SpecialAssessment

    n = SpecialAssessment.objects.count()
    return f"{_n(n, 'special assessment')} levied." if n else "None levied."


def _payment_plans(ctx) -> str:
    from ledger.models import PaymentPlan

    n = PaymentPlan.objects.for_user(ctx.user).count()
    return f"{_n(n, 'plan')} on record." if n else "No payment plans yet."


def _interest(ctx) -> str:
    from ledger import interest

    policy = interest.current_policy()
    if not policy.rate_bp:
        return "No rate recorded: nothing is charged."
    return f"{interest.monthly_rate_text(policy.rate_bp)}% a month on balances past due."


def _cashflow(ctx) -> str:
    return "The next 12 months of operating and reserve cash, with what-ifs."


def _reimbursements(ctx) -> str:
    from operations.models_payouts import ReimbursementClaim

    n = ReimbursementClaim.objects.for_user(ctx.user).count()
    return f"{_n(n, 'claim')} on record." if n else "No claims yet."


def _vendor_checks(ctx) -> str:
    from operations.models_payouts import VendorCheck

    n = VendorCheck.objects.count()
    return f"{_n(n, 'check')} in the register." if n else "No checks written yet."


def _vendors(ctx) -> str:
    from operations.models import Vendor

    active = Vendor.objects.filter(active=True).count()
    line = f"{_n(active, 'active vendor')}."
    try:
        from operations.onboarding_models import OnboardingRound, RoundStatus

        if features.enabled("vendor_onboarding", ctx.request):
            waiting = OnboardingRound.objects.filter(status=RoundStatus.SUBMITTED).count()
            line += f" {waiting} onboarding waiting for review." if waiting else ""
    except ImportError:  # pragma: no cover - onboarding not installed
        pass
    return line


def _capital_projects(ctx) -> str:
    from operations.models_options import CapitalProject

    n = CapitalProject.objects.exclude(state__in=["completed", "cancelled"]).count()
    return f"{_n(n, 'project')} planned or under way." if n else "No open projects."


def _insurance_claims(ctx) -> str:
    from operations.models_options import InsuranceClaim

    n = InsuranceClaim.objects.exclude(state__in=["closed", "denied", "paid"]).count()
    return f"{_n(n, 'open claim')}." if n else "No open claims."


@dataclass(frozen=True)
class Card:
    group: str
    label: str  # the page's <h1> (or the section's heading, with an anchor)
    url_name: str
    icon: str
    permission: str
    figure: object  # callable(ctx) -> str
    # My items sidebar keys whose items wait on this page.
    counts: tuple[str, ...] = field(default=())
    # A section of a page rather than a page of its own (Year-end package).
    anchor: str = ""


MONEY_IN, MONEY_OUT, BOOKS, PLANNING, YEAR_END = (
    "Money in",
    "Money out",
    "Books",
    "Planning",
    "Year end and tax",
)
GROUPS = (MONEY_IN, MONEY_OUT, BOOKS, PLANNING, YEAR_END)

# Every finance component. A later component adds a card here (and, without a
# menu entry of its own, its URL name to navigation.SUBPAGES["finance_overview"]).
CARDS: tuple[Card, ...] = (
    # Money in
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
        "Payment plans",
        "payment_plans",
        "clock",
        "finance.view",
        _payment_plans,
        ("payment_plans",),
    ),
    Card(
        MONEY_IN,
        "Special assessments",
        "special_assessments",
        "dollar",
        "special_assessment.view",
        _special_assessments,
        ("special_assessments",),
    ),
    # Online payments holds the automatic payment reviews and failures (D47, D74).
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
        "Interest on overdue balances",
        "interest",
        "dollar",
        "finance.view",
        _interest,
        ("interest",),
    ),
    Card(
        MONEY_IN, "Resale certificates", "resales", "file", "resale.manage", _resales, ("resales",)
    ),
    # Money out
    Card(MONEY_OUT, "Invoices", "invoices", "receipt", "finance.view", _invoices, ("invoices",)),
    Card(
        MONEY_OUT,
        "Checks",
        "vendor_checks",
        "receipt",
        "vendor_check.view",
        _vendor_checks,
        ("vendor_checks",),
    ),
    Card(
        MONEY_OUT,
        "Reimbursements",
        "reimbursements",
        "receipt",
        "reimbursement.view_all",
        _reimbursements,
        ("reimbursements",),
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
    Card(MONEY_OUT, "Vendors", "vendors", "users", "finance.view", _vendors, ("vendors",)),
    # Books
    Card(
        BOOKS,
        "Month and year close",
        "books_close",
        "check-circle",
        "books.view",
        _books_close,
        ("books_close",),
    ),
    Card(
        BOOKS,
        "Bank accounts and funds",
        "books_accounts",
        "wallet",
        "books.view",
        _books_accounts,
        ("books_accounts",),
    ),
    Card(
        BOOKS,
        "Bank categories and rules",
        "books_coding",
        "list",
        "books.view",
        _books_coding,
        ("books_coding",),
    ),
    Card(
        BOOKS,
        "Financial statements",
        "financial_statements",
        "chart",
        "statements.view",
        _financial_statements,
        ("financial_statements",),
    ),
    Card(
        BOOKS, "Accounting", "accounting", "receipt", "finance.view", _accounting, ("accounting",)
    ),
    Card(BOOKS, "Bank feed", "bank_feed", "activity", "bank_feed.view", _bank_feed, ("bank_feed",)),
    Card(BOOKS, "Reports", "reports", "chart", "reports.view", _reports, ("reports",)),
    # Planning
    Card(PLANNING, "Budgets", "budgets", "chart", "finance.view", _budgets, ("budgets",)),
    Card(
        PLANNING,
        "Reserve plan",
        "reserve_plan",
        "chart",
        "reserves.view",
        _reserve_plan,
        ("reserve_plan",),
    ),
    Card(
        PLANNING,
        "Cash-flow forecast",
        "cashflow",
        "chart",
        "finance.view",
        _cashflow,
        ("cashflow",),
    ),
    Card(
        PLANNING,
        "Capital projects",
        "capital_projects",
        "wrench",
        "capital_project.view",
        _capital_projects,
        ("capital_projects",),
    ),
    Card(
        PLANNING,
        "Insurance claims",
        "insurance_claims",
        "shield",
        "insurance.view",
        _insurance_claims,
        ("insurance_claims",),
    ),
    # Year end and tax
    Card(
        YEAR_END,
        "Year-end package",
        "financial_statements",
        "file",
        "books.package",
        _year_end,
        (),
        anchor="#package",
    ),
    Card(YEAR_END, "Tax worksheet", "tax_worksheet", "file", "tax.view", _tax, ("tax_worksheet",)),
)
URL_NAMES = tuple(dict.fromkeys(c.url_name for c in CARDS))


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
        self.today = timezone.localdate()
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
                "url": reverse(card.url_name) + card.anchor,
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
        **_fund_strip(request),
        "groups": [
            {"title": g, "cards": [r for r in rows if r["group"] == g]}
            for g in GROUPS
            if any(r["group"] == g for r in rows)
        ],
        "tiles": tiles,
        "waiting": sum(r["count"] for r in rows),
        "today": timezone.localdate(),
    }


def _fund_strip(request) -> dict:
    """Each fund's cash at the end of the last closed month, when the books
    are set up (D69-D71): the same figures the forecast and the owners'
    summary read (``closed_fund_positions``)."""
    if not features.enabled("books", request) or not policy.can(request.user, "books.view"):
        return {"funds": [], "funds_as_of": None}
    from ledger.books import close, funds

    found = close.closed_fund_positions()
    if found is None:
        return {"funds": [], "funds_as_of": None}
    end, positions = found
    return {
        "funds": [
            {"label": funds.FUND_LABELS.get(fund, fund), "cash": pos["cash"]}
            for fund, pos in positions.items()
        ],
        "funds_as_of": end,
    }

"""Funds, the association's bank accounts in the books, and categories (D69).

Every association bank account (a bank feed connection) belongs to a fund:
operating or reserve (Declaration §8.7: a reserve the board determines). One
account is the primary operating account: deposits, Stripe payouts, vendor
payments and owner checks are in it unless matched to a line of another
account. Each account has its balance on the books' start date (Accounting
setup's opening date), so every month's reconciliation starts from a known
figure.

Categories are what bank lines are coded to: interest income, bank fees,
insurance or utilities paid by bank debit, a transfer between the
association's accounts... kept apart from assessments and the other owner
charges, which stay in the owner ledger.
"""

from __future__ import annotations

import datetime
import re

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from core.policy import SYSTEM, require
from ledger.books.models import BankAccountSetup, CategoryKind, Fund, GLCategory


class BooksError(ValueError):
    """A refusal with a message safe to show."""


def _user(actor):
    return None if actor is SYSTEM else actor


FUND_LABELS = dict(Fund.choices)
FUNDS = [Fund.OPERATING, Fund.RESERVE]
TRANSFER_ACCOUNT = "3900 Transfers between funds"
INTERFUND_ACCOUNT = "3950 Due to and from other funds"
OPENING_EQUITY = "3000 Fund balance at the start of the books"

# key, name, account, kind, exempt-function income (Form 1120-H)
DEFAULT_CATEGORIES = [
    ("interest_income", "Interest income", "4500 Interest income", "income", False),
    ("refunds_received", "Refunds received from vendors", "4800 Refunds received", "income", False),
    ("other_income", "Other income", "4900 Other income", "income", False),
    ("bank_fees", "Bank fees", "6100 Bank fees", "expense", False),
    (
        "processing_fees",
        "Payment processing fees",
        "6150 Payment processing fees",
        "expense",
        False,
    ),
    ("insurance", "Insurance", "5300 Insurance", "expense", False),
    ("utilities", "Utilities", "5600 Utilities", "expense", False),
    ("taxes", "Taxes and government fees", "6400 Taxes and fees", "expense", False),
    (
        "penalties_paid",
        "Penalties and interest paid",
        "6900 Penalties and interest paid",
        "expense",
        False,
    ),
    ("adjustments", "Adjustments", "6950 Adjustments", "expense", False),
    (
        "transfer",
        "Transfer between the association's accounts",
        TRANSFER_ACCOUNT,
        "transfer",
        False,
    ),
]


def ensure_default_categories() -> None:
    """The standard categories (the migration adds them; tests that flush
    the database call this)."""
    for key, name, account, kind, exempt in DEFAULT_CATEGORIES:
        GLCategory.objects.get_or_create(
            key=key,
            defaults={"name": name, "account": account, "kind": kind, "exempt_function": exempt},
        )


# --- Account kinds ------------------------------------------------------------------------

ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = "asset", "liability", "equity", "income", "expense"
_BY_DIGIT = {"1": ASSET, "2": LIABILITY, "3": EQUITY, "4": INCOME}


def account_kind(account: str, hint: str = "") -> str:
    """What an account is, from its number's first digit (1 assets, 2
    liabilities, 3 fund balance, 4 income, 5-9 expenses), else the hint."""
    text = (account or "").strip()
    if text[:1].isdigit():
        return _BY_DIGIT.get(text[0], EXPENSE)
    if hint == CategoryKind.TRANSFER:
        return EQUITY
    return hint or EXPENSE


# --- Books start and bank accounts --------------------------------------------------------


def books_start() -> datetime.date | None:
    """The books' start date: the opening balance date in Accounting setup.
    Balances are as at the end of that day."""
    from ledger.models import AccountingSetup

    return AccountingSetup.get().opening_cash_on


def first_month() -> datetime.date | None:
    """The first day of the first month the books cover."""
    start = books_start()
    if start is None:
        return None
    return (start + datetime.timedelta(days=1)).replace(day=1)


def accounts():
    """The association's bank accounts in the books (connected or not)."""
    return list(BankAccountSetup.objects.select_related("connection"))


def primary():
    return BankAccountSetup.objects.filter(is_primary=True).select_related("connection").first()


def setup_for(connection_id) -> BankAccountSetup | None:
    return (
        BankAccountSetup.objects.filter(connection_id=connection_id)
        .select_related("connection")
        .first()
    )


def default_gl(fund: str, primary_account: bool) -> str:
    from ledger.models import AccountingSetup

    if fund == Fund.OPERATING and primary_account:
        return AccountingSetup.get().accounts()["cash"]
    return "1010 Reserve bank account" if fund == Fund.RESERVE else "1005 Operating bank account"


_GL_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z .&()/,'-]{0,79}$")


def _clean_gl(value: str) -> str:
    value = " ".join((value or "").replace("=", " ").split())[:80]
    if not value or not _GL_RE.match(value):
        raise BooksError("Give the account its number and name, e.g. 1010 Reserve savings.")
    return value


def _locked_by_close(day: datetime.date | None) -> bool:
    from ledger.books import periods

    return day is not None and periods.any_closed_after(day)


def save_account(
    connection,
    *,
    fund: str,
    gl_account: str = "",
    is_primary: bool = False,
    opening_balance_cents: int | None,
    actor,
    request=None,
) -> BankAccountSetup:
    """Put a bank account in the books, or change its fund, account or
    opening balance. Refused once a month is closed (the closed figures
    rest on them): reopen first."""
    require(actor, "books.setup")
    if fund not in FUND_LABELS:
        raise BooksError("Choose the fund this account belongs to.")
    if is_primary and fund != Fund.OPERATING:
        raise BooksError("The primary account (where deposits go) must be an operating account.")
    with transaction.atomic():
        existing = (
            BankAccountSetup.objects.select_for_update().filter(connection=connection).first()
        )
        changed_money = existing is None or (
            existing.fund != fund or existing.opening_balance_cents != opening_balance_cents
        )
        if changed_money and _locked_by_close(books_start()):
            raise BooksError(
                "A month is closed: the fund and opening balance of an account cannot change. "
                "Reopen the months first, or post a correction in the open month."
            )
        gl = _clean_gl(gl_account) if gl_account else default_gl(fund, is_primary)
        if is_primary:
            for other in BankAccountSetup.objects.exclude(connection=connection).filter(
                is_primary=True
            ):
                other.is_primary = False
                other.updated_by = _user(actor)
                other.updated_at = timezone.now()
                other.save()
        if existing is None:
            setup = BankAccountSetup.objects.create(
                connection=connection,
                fund=fund,
                gl_account=gl,
                is_primary=is_primary,
                opening_balance_cents=opening_balance_cents,
                updated_by=_user(actor),
            )
        else:
            setup = existing
            setup.fund = fund
            setup.gl_account = gl
            setup.is_primary = is_primary
            setup.opening_balance_cents = opening_balance_cents
            setup.updated_by = _user(actor)
            setup.updated_at = timezone.now()
            setup.save()
    record_event(
        "books.account_saved",
        actor=_user(actor),
        request=request,
        target=setup,
        metadata={
            "connection_id": connection.pk,
            "fund": fund,
            "primary": bool(is_primary),
            "opening_set": opening_balance_cents is not None,
        },
    )
    return setup


def fund_of_connection(connection_id) -> str:
    setup = setup_for(connection_id)
    return setup.fund if setup else Fund.OPERATING


def cash_account_of(connection_id) -> tuple[str, str]:
    """(GL account, fund) of a bank account; the primary's for unknown ones."""
    from ledger.models import AccountingSetup

    setup = setup_for(connection_id) if connection_id else None
    if setup is None:
        setup = primary()
    if setup is None:
        return AccountingSetup.get().accounts()["cash"], Fund.OPERATING
    return setup.gl_account, setup.fund


# --- Categories -----------------------------------------------------------------------------


def active_categories():
    return list(GLCategory.objects.filter(active=True))


_KEY_RE = re.compile(r"[^a-z0-9]+")


def save_category(
    *,
    name: str,
    account: str,
    kind: str,
    fund: str = "",
    exempt_function: bool = False,
    active: bool = True,
    category: GLCategory | None = None,
    actor,
    request=None,
) -> GLCategory:
    require(actor, "books.setup")
    name = " ".join((name or "").split())[:80]
    if not name:
        raise BooksError("Name the category.")
    if kind not in CategoryKind.values:
        raise BooksError("Choose what kind of category this is.")
    if fund and fund not in FUND_LABELS:
        raise BooksError("Choose a fund, or leave it to the bank account's fund.")
    account = _clean_gl(account)
    if kind == CategoryKind.TRANSFER and account_kind(account) != EQUITY:
        raise BooksError("A transfer category needs a fund-balance account (3xxx).")
    with transaction.atomic():
        if category is None:
            base = _KEY_RE.sub("_", name.lower()).strip("_")[:30] or "category"
            key, n = base, 2
            while GLCategory.objects.filter(key=key).exists():
                key, n = f"{base[:26]}_{n}", n + 1
            category = GLCategory(key=key)
        elif category.kind != kind and category.rules.exists():
            raise BooksError("Coding rules use this category; its kind cannot change.")
        category.name = name
        category.account = account
        category.kind = kind
        category.fund = fund
        category.exempt_function = bool(exempt_function)
        category.active = bool(active)
        category.updated_by = _user(actor)
        category.updated_at = timezone.now()
        category.save()
    record_event(
        "books.category_saved",
        actor=_user(actor),
        request=request,
        target=category,
        metadata={"category_id": category.pk, "kind": kind, "active": bool(active)},
    )
    return category

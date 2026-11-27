"""Synthetic books for the demo community (D69-D71): an operating and a
reserve account, bank lines coded by rules, the prior fiscal year closed with
its year-end package, closed months this year, and last month left open with
exceptions for the treasurer to resolve. Fictional data only; run by
``seed_demo`` (oak_creek) through ``manage.py seed_demo_books``.

The past months are recorded as closed directly (a demo shortcut with each
month's real check results in its summary); a live portal closes them
through the checks (ledger.books.close).
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from core.policy import SYSTEM
from core.scoping import system_access

DEMO = "DEMO - replace"


def _months(first: datetime.date, last: datetime.date):
    from ledger.books import periods

    return periods.months_between(first, last)


def seed(today: datetime.date | None = None) -> dict:
    from community import services as community
    from ledger.books import close, coding, funds, journal, package, periods
    from ledger.books.models import CloseHow, LineSign, PeriodClose, PeriodKind
    from ledger.models import AccountingSetup, BankBalance, BankConnection, BankTransaction

    today = today or timezone.localdate()
    if BankConnection.objects.filter(display_name__endswith="(demo)").exists():
        return {"skipped": True}
    funds.ensure_default_categories()
    with system_access("demo books"):
        fy_now = periods.fiscal_year_of(today)
        prior_start, _prior_end = periods.year_bounds(fy_now - 1)
        start = prior_start - datetime.timedelta(days=1)
        setup = AccountingSetup.get()
        setup.opening_cash_on = start
        setup.opening_cash_cents = 2500000
        setup.save()
        board_user = _a_board_user()
        if board_user is None:
            return {"skipped": True}
        op = BankConnection.objects.create(
            provider="file",
            display_name="Operating checking (demo)",
            institution_name="Example Community Bank",
            account_last_four="0101",
            created_by=board_user,
        )
        rs = BankConnection.objects.create(
            provider="file",
            display_name="Reserve savings (demo)",
            institution_name="Example Community Bank",
            account_last_four="0202",
            created_by=board_user,
        )
        funds.save_account(
            op, fund="operating", is_primary=True, opening_balance_cents=2500000, actor=SYSTEM
        )
        funds.save_account(rs, fund="reserve", opening_balance_cents=4000000, actor=SYSTEM)
        community.set_rule(
            key="books.reserve_monthly_cents", value="50000", citation=DEMO, actor=SYSTEM
        )
        cats = {c.key: c for c in funds.active_categories()}
        for text, sign, key in (
            ("INTEREST", LineSign.IN, "interest_income"),
            ("SERVICE CHARGE", LineSign.OUT, "bank_fees"),
            ("TRANSFER TO SAVINGS", LineSign.OUT, "transfer"),
            ("TRANSFER FROM CHECKING", LineSign.IN, "transfer"),
            ("EXAMPLE MUTUAL INS", LineSign.OUT, "insurance"),
        ):
            coding.save_rule(
                name=text.title(),
                contains=text,
                sign=sign,
                category=cats[key],
                actor=SYSTEM,
                apply_now=False,
            )
        last_month_end = today.replace(day=1) - datetime.timedelta(days=1)
        n = 0
        keys = _months(start + datetime.timedelta(days=1), last_month_end)
        for key in keys:
            m_start, m_end = periods.month_bounds(key)
            rows = [
                (rs, m_end, 1800 + (m_start.month * 7), "INTEREST PAYMENT"),
                (op, m_start + datetime.timedelta(days=4), -1200, "MONTHLY SERVICE CHARGE"),
                (op, m_start + datetime.timedelta(days=14), -50000, "ONLINE TRANSFER TO SAVINGS"),
                (rs, m_start + datetime.timedelta(days=14), 50000, "TRANSFER FROM CHECKING"),
            ]
            if m_start.month in (1, 7):
                rows.append(
                    (
                        op,
                        m_start + datetime.timedelta(days=9),
                        -185000,
                        "EXAMPLE MUTUAL INS PREMIUM",
                    )
                )
            for conn, day, cents, text in rows:
                n += 1
                BankTransaction.objects.create(
                    connection=conn,
                    bank_id=f"demo-{n}",
                    posted_on=day,
                    amount_cents=cents,
                    description=text,
                    source="ofx",
                )
        coding.apply_rules()
        # Month-end balances the bank would report: the books less what is in
        # transit (the demo's deposits and checks are not on these accounts).
        last_open = keys[-1] if keys else None
        items = journal.cash_items(today)
        for key in keys:
            _m_start, m_end = periods.month_bounds(key)
            for s in funds.accounts():
                if key == last_open and s.connection_id == op.pk:
                    continue  # last month: the operating balance is still to enter
                rec = journal.reconcile_account(s, m_end, items)
                bank = rec.book_cents - rec.outstanding_in - rec.outstanding_out + rec.bank_ahead
                BankBalance.objects.create(
                    connection=s.connection, as_of=m_end, balance_cents=bank, source="ofx"
                )
        # Last month: a line nobody has coded yet.
        if last_open:
            _m_start, m_end = periods.month_bounds(last_open)
            n += 1
            BankTransaction.objects.create(
                connection=op,
                bank_id=f"demo-{n}",
                posted_on=m_end - datetime.timedelta(days=3),
                amount_cents=-4500,
                description="CHECK 1043",
                source="ofx",
            )
        closed = 0
        for key in keys[:-1]:
            ev = close.evaluate_month(key)
            m_start, m_end = periods.month_bounds(key)
            PeriodClose.objects.create(
                kind=PeriodKind.MONTH,
                period=key,
                starts_on=m_start,
                ends_on=m_end,
                how=CloseHow.MANUAL,
                summary={**ev.summary(), "demo": True},
            )
            closed += 1
        prior = periods.year_key(fy_now - 1)
        y_start, y_end = periods.year_bounds(fy_now - 1)
        if last_open and y_end < periods.month_bounds(last_open)[0]:
            PeriodClose.objects.create(
                kind=PeriodKind.YEAR,
                period=prior,
                starts_on=y_start,
                ends_on=y_end,
                how=CloseHow.MANUAL,
                summary={"demo": True},
            )
            package.build(fy_now - 1, actor=SYSTEM, automatic=True)
        if last_open:
            close._record_run(close.evaluate_month(last_open), False)
    return {"accounts": 2, "lines": n, "closed_months": closed}


def _a_board_user():
    from community.models import BoardSeat

    seat = (
        BoardSeat.objects.active()
        .filter(person__user__isnull=False)
        .select_related("person__user")
        .first()
    )
    if seat is not None:
        return seat.person.user
    from django.contrib.auth import get_user_model

    return get_user_model().objects.order_by("id").first()

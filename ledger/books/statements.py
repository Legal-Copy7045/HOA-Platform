"""Financial statements (D71): balance sheet by fund, income and expenses
with budget against actual (monthly and year to date), reserve activity,
unpaid balances by age, the annual accounting figure, and the tax worksheet.

Basis: owner charges are income when charged (receivables), expenses when
paid (vendor payments, coded bank lines): a modified cash basis, stated on
every statement. All figures are integer cents from the journal
(``ledger.accounting.journal_lines``) and the opening balances.
"""

from __future__ import annotations

import csv
import datetime
import io
from collections import defaultdict

from django.db.models import Sum

from audit.services import record_event
from core import pdf
from core.lists import safe_cell
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import internal, system_access
from ledger.books import funds, journal, periods
from ledger.books.funds import ASSET, EQUITY, EXPENSE, FUND_LABELS, FUNDS, INCOME, LIABILITY
from ledger.books.models import CategoryKind, Fund, PeriodKind

BASIS = (
    "Basis: owner charges are income when charged; expenses are recorded when paid "
    "(modified cash basis). Figures are from the portal's books."
)


def _user(actor):
    return None if actor is SYSTEM else actor


def _cents(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}" if value >= 0 else f"-{_cents(-value)}"


# --- Balance sheet --------------------------------------------------------------------------------


def _owner_split(as_of: datetime.date) -> tuple[int, int, int]:
    """(owed by homes, prepaid or credit held for homes, homes owing) at the
    end of ``as_of``."""
    from ledger.models import LedgerEntry

    with system_access("books: owner balances"):
        rows = (
            LedgerEntry.objects.filter(posted_on__lte=as_of)
            .values("lot_id")
            .annotate(s=Sum("amount_cents"))
            .values_list("s", flat=True)
        )
        rows = list(rows)
    owed = sum(s for s in rows if s > 0)
    held = -sum(s for s in rows if s < 0)
    return owed, held, sum(1 for s in rows if s > 0)


@internal("books: balance_sheet")
def balance_sheet(as_of: datetime.date) -> dict:
    from ledger.models import AccountingSetup

    b = journal.balances_as_of(as_of)
    receivable = AccountingSetup.get().accounts()["receivable"]
    owed, held, owing = _owner_split(as_of)
    out = {}
    for fund in FUNDS:
        assets = {a: v for a, v in b.by_kind(ASSET, fund).items() if v}
        liabilities = {a: -v for a, v in b.by_kind(LIABILITY, fund).items() if v}
        if fund == Fund.OPERATING and (receivable in assets or owed or held):
            total = assets.pop(receivable, 0)
            # Homes that owe are an asset; credit held for homes is owed back.
            assets["Owner receivables (homes that owe)"] = owed + total - (owed - held)
            if held:
                liabilities["Owner prepayments and credits"] = held
        equity = {a: -v for a, v in b.by_kind(EQUITY, fund).items() if v}
        net_income = -(b.total(INCOME, fund) + b.total(EXPENSE, fund))
        fund_balance = sum(equity.values()) + net_income
        out[fund] = {
            "label": FUND_LABELS[fund],
            "assets_rows": sorted(assets.items()),
            "liabilities_rows": sorted(liabilities.items()),
            "equity_rows": sorted(equity.items()),
            "net_income": net_income,
            "assets": sum(assets.values()),
            "liabilities": sum(liabilities.values()),
            "fund_balance": fund_balance,
        }
    return {
        "as_of": as_of,
        "funds": out,
        "total_assets": sum(f["assets"] for f in out.values()),
        "total_liabilities": sum(f["liabilities"] for f in out.values()),
        "total_fund_balance": sum(f["fund_balance"] for f in out.values()),
        "homes_owing": owing,
    }


# --- Income and expenses --------------------------------------------------------------------------


def _months(start: datetime.date, end: datetime.date) -> list[tuple[datetime.date, datetime.date]]:
    out = []
    for key in periods.months_between(start, end):
        s, e = periods.month_bounds(key)
        out.append((max(s, start), min(e, end)))
    return out


@internal("books: income_statement")
def income_statement(start: datetime.date, end: datetime.date, fund: str | None = None) -> dict:
    """Income and expenses by account for the period, with one column per
    month and the total; transfers between funds shown apart."""
    months = _months(start, end)
    per_month = [journal.activity(s, e) for s, e in months]
    rows: dict[str, dict] = {}
    for kind in (INCOME, EXPENSE):
        accounts: dict[str, list[int]] = defaultdict(lambda: [0] * len(months))
        for i, b in enumerate(per_month):
            for account, net in b.by_kind(kind, fund).items():
                accounts[account][i] += -net if kind == INCOME else net
        rows[kind] = [
            {"account": a, "months": vals, "total": sum(vals)}
            for a, vals in sorted(accounts.items())
            if any(vals)
        ]
    transfers = [0] * len(months)
    for i, b in enumerate(per_month):
        for account, net in b.by_kind(EQUITY, fund).items():
            if account == funds.TRANSFER_ACCOUNT:
                transfers[i] += -net
    income = [sum(r["months"][i] for r in rows[INCOME]) for i in range(len(months))]
    expense = [sum(r["months"][i] for r in rows[EXPENSE]) for i in range(len(months))]
    return {
        "start": start,
        "end": end,
        "fund": fund,
        "months": [s for s, _e in months],
        "income_rows": rows[INCOME],
        "expense_rows": rows[EXPENSE],
        "income": income,
        "expense": expense,
        "net": [a - b for a, b in zip(income, expense, strict=True)],
        "transfers": transfers,
        "income_total": sum(income),
        "expense_total": sum(expense),
        "net_total": sum(income) - sum(expense),
        "transfers_total": sum(transfers),
    }


def _budget_account(line, gl: dict) -> str | None:
    from ledger.models import IncomeSource, LineKind

    if line.kind == LineKind.EXPENSE and line.category_id:
        return f"{line.category.code} {line.category.name}".strip()
    return {
        IncomeSource.ASSESSMENTS: gl["assessment"],
        IncomeSource.RESALE_FEES: gl["resale_fee"],
    }.get(line.income_source)


@internal("books: budget_vs_actual")
def budget_vs_actual(fiscal_year: int, through: datetime.date | None = None) -> dict:
    """Each adopted budget line against the year's actuals, monthly and to
    date; actual figures no budget line covers are listed as unbudgeted."""
    from ledger.models import AccountingSetup, Budget, BudgetStatus, IncomeSource, LineKind

    start, end = periods.year_bounds(fiscal_year)
    if through is not None:
        end = min(end, through)
    stmt = income_statement(start, end)
    gl = AccountingSetup.get().accounts()
    budget = Budget.objects.filter(fiscal_year=fiscal_year, status=BudgetStatus.ADOPTED).first()
    actual = {r["account"]: r for r in stmt["income_rows"] + stmt["expense_rows"]}
    used: set[str] = set()
    rows = []
    n = len(stmt["months"])
    if budget is not None:
        for line in budget.lines.select_related("category"):
            planned = line.monthly_cents()[:n]
            account = _budget_account(line, gl)
            if line.kind == LineKind.INCOME and line.income_source == IncomeSource.OTHER:
                accounts = [gl["other_charge"], gl["late_fee"]]
            else:
                accounts = [account] if account else []
            months = [0] * n
            for a in accounts:
                if a in actual:
                    used.add(a)
                    months = [x + y for x, y in zip(months, actual[a]["months"], strict=True)]
            if (
                line.kind == LineKind.EXPENSE
                and not any(months)
                and line.category
                and "reserve" in line.category.name.lower()
            ):
                months = list(income_statement(start, end, Fund.RESERVE)["transfers"])
            rows.append(
                {
                    "name": line.name,
                    "kind": line.kind,
                    "planned": planned,
                    "actual": months,
                    "planned_total": sum(planned),
                    "actual_total": sum(months),
                    "variance": sum(months) - sum(planned),
                }
            )
    unbudgeted = [
        {"name": r["account"], "actual_total": r["total"], "kind": kind}
        for kind, key in ((LineKind.INCOME, "income_rows"), (LineKind.EXPENSE, "expense_rows"))
        for r in stmt[key]
        if r["account"] not in used
    ]
    return {
        "fiscal_year": fiscal_year,
        "budget": budget,
        "months": stmt["months"],
        "rows": rows,
        "unbudgeted": unbudgeted,
    }


# --- Reserve activity, aging, the annual accounting -----------------------------------------------


def _fund_balance(fund: str, as_of: datetime.date) -> int:
    first = funds.books_start()
    if first is None or as_of < first:
        return 0
    b = journal.balances_as_of(as_of)
    return b.total(ASSET, fund) + b.total(LIABILITY, fund)


def reserve_activity(start: datetime.date, end: datetime.date) -> dict:
    b = journal.activity(start, end)
    fund = Fund.RESERVE
    equity = b.by_kind(EQUITY, fund)
    opening = _fund_balance(fund, start - datetime.timedelta(days=1))
    transfers = -equity.get(funds.TRANSFER_ACCOUNT, 0)
    interfund = -equity.get(funds.INTERFUND_ACCOUNT, 0)
    income = -b.total(INCOME, fund)
    expense = b.total(EXPENSE, fund)
    other = -sum(
        v for a, v in equity.items() if a not in (funds.TRANSFER_ACCOUNT, funds.INTERFUND_ACCOUNT)
    )
    closing = _fund_balance(fund, end)
    return {
        "opening": opening,
        "transfers_in": transfers,
        "income": income,
        "expenses": expense,
        "interfund": interfund,
        "other": other,
        "closing": closing,
        "adds_up": opening + transfers + income - expense + interfund + other == closing,
    }


@internal("books: aging_totals")
def aging_totals(as_of: datetime.date) -> dict:
    from community.models import Lot
    from ledger.services import BUCKET_LABELS, BUCKETS, aging

    totals = dict.fromkeys(BUCKETS, 0)
    homes = 0
    with system_access("books: aging"):
        lot_ids = list(Lot.objects.values_list("pk", flat=True))
    for lot_id in lot_ids:
        a = aging(lot_id, as_of)
        if any(a[k] for k in BUCKETS):
            homes += 1
        for k in BUCKETS:
            totals[k] += a[k]
    return {
        "as_of": as_of,
        "rows": [(BUCKET_LABELS[k], totals[k]) for k in BUCKETS],
        "total": sum(totals.values()),
        "homes": homes,
    }


@internal("books: annual_accounting")
def annual_accounting(fiscal_year: int) -> dict:
    """The Declaration's annual accounting figure (§8.8 style): the net excess
    or deficit of income over expenditures plus reserves, for the operating
    fund. The surplus candidate for D67; the board decides any distribution."""
    start, end = periods.year_bounds(fiscal_year)
    op = income_statement(start, end, Fund.OPERATING)
    reserves = -op["transfers_total"]  # money moved out of operating into reserves
    net = op["income_total"] - op["expense_total"] - reserves
    return {
        "fiscal_year": fiscal_year,
        "income_cents": op["income_total"],
        "expenditures_cents": op["expense_total"],
        "reserves_cents": reserves,
        "net_cents": net,
        "closed": periods.is_closed_period(periods.year_key(fiscal_year), PeriodKind.YEAR),
    }


@internal("books: tax_worksheet")
def tax_worksheet(fiscal_year: int) -> dict:
    """Totals for the tax preparer: exempt-function income (from members as
    members: assessments, owner fees) against other income (interest...).
    Homeowners associations commonly file Form 1120-H (IRC § 528); the
    preparer confirms the form."""
    from ledger.books.models import GLCategory
    from ledger.models import AccountingSetup

    start, end = periods.year_bounds(fiscal_year)
    stmt = income_statement(start, end)
    gl = AccountingSetup.get().accounts()
    member = {
        gl[k]
        for k in (
            "assessment",
            "late_fee",
            "resale_fee",
            "other_charge",
            "credit",
            "card_surcharge",
        )
    }
    member |= set(
        GLCategory.objects.filter(exempt_function=True, kind=CategoryKind.INCOME).values_list(
            "account", flat=True
        )
    )
    exempt = sum(r["total"] for r in stmt["income_rows"] if r["account"] in member)
    other_rows = [r for r in stmt["income_rows"] if r["account"] not in member]
    other = sum(r["total"] for r in other_rows)
    gross = exempt + other
    return {
        "fiscal_year": fiscal_year,
        "exempt_function_cents": exempt,
        "other_income_rows": [(r["account"], r["total"]) for r in other_rows],
        "other_income_cents": other,
        "gross_income_cents": gross,
        "exempt_share_percent": round(exempt * 100 / gross) if gross else None,
        "passes_60_percent": gross == 0 or exempt * 100 >= 60 * gross,
        "expenses_cents": stmt["expense_total"],
        "note": (
            "Homeowners associations commonly file Form 1120-H (Internal Revenue Code § 528): "
            "at least 60% of gross income must be exempt-function income and at least 90% of "
            "spending must be on the association's property. Other income (such as interest) "
            "is taxed after a $100 specific deduction. Have the tax preparer confirm the form "
            "(1120-H or 1120) and the figures."
        ),
    }


# --- One set of statements ------------------------------------------------------------------------


def period_for(*, month: str = "", year: int | None = None) -> dict:
    """The period a statements page shows: a month or a fiscal year, and
    whether it is closed."""
    if month:
        start, end = periods.month_bounds(month)
        return {
            "kind": PeriodKind.MONTH,
            "key": month,
            "label": f"{start:%B %Y}",
            "start": start,
            "end": end,
            "closed": periods.is_closed_period(month),
            "fiscal_year": periods.fiscal_year_of(start),
        }
    start, end = periods.year_bounds(year)
    return {
        "kind": PeriodKind.YEAR,
        "key": periods.year_key(year),
        "label": f"Fiscal year {year}",
        "start": start,
        "end": end,
        "closed": periods.is_closed_period(periods.year_key(year), PeriodKind.YEAR),
        "fiscal_year": year,
    }


@internal("books: statement_set")
def statement_set(period: dict) -> dict:
    fy = period["fiscal_year"]
    fy_start, _fy_end = periods.year_bounds(fy)
    end = period["end"]
    return {
        "period": period,
        "basis": BASIS,
        "balance_sheet": balance_sheet(end),
        "income": income_statement(period["start"], end),
        "income_by_fund": {f: income_statement(period["start"], end, f) for f in FUNDS},
        "year_to_date": income_statement(fy_start, end),
        "budget": budget_vs_actual(fy, through=end),
        "reserve": reserve_activity(period["start"], end),
        "aging": aging_totals(end),
        "annual": annual_accounting(fy) if period["kind"] == PeriodKind.YEAR else None,
        "tax": tax_worksheet(fy) if period["kind"] == PeriodKind.YEAR else None,
    }


# --- CSV and PDF ----------------------------------------------------------------------------------


def csv_rows(s: dict) -> list[list[str]]:
    p = s["period"]
    status = "closed" if p["closed"] else "not closed"
    rows: list[list[str]] = [
        ["Financial statements", p["label"], status],
        [s["basis"]],
        [],
        ["Balance sheet", f"at {p['end']:%Y-%m-%d}", "Fund", "Amount"],
    ]
    for f in s["balance_sheet"]["funds"].values():
        for heading, key in (("Assets", "assets_rows"), ("Liabilities", "liabilities_rows")):
            for account, cents in f[key]:
                rows.append([heading, account, f["label"], _cents(cents)])
        rows.append(["Fund balance", "", f["label"], _cents(f["fund_balance"])])
    rows += [[], ["Income and expenses", f"{p['start']:%Y-%m-%d} to {p['end']:%Y-%m-%d}"]]
    inc = s["income"]
    rows.append(["Account", *[f"{m:%b %Y}" for m in inc["months"]], "Total"])
    for label, key in (("Income", "income_rows"), ("Expenses", "expense_rows")):
        rows.append([label])
        for r in inc[key]:
            rows.append([r["account"], *[_cents(v) for v in r["months"]], _cents(r["total"])])
    rows.append(["Net income", *[_cents(v) for v in inc["net"]], _cents(inc["net_total"])])
    rows += [[], ["Budget against actual", f"fiscal year {s['budget']['fiscal_year']}"]]
    rows.append(["Line", "Budget to date", "Actual to date", "Variance"])
    for r in s["budget"]["rows"]:
        rows.append(
            [
                r["name"],
                _cents(r["planned_total"]),
                _cents(r["actual_total"]),
                _cents(r["variance"]),
            ]
        )
    for r in s["budget"]["unbudgeted"]:
        rows.append([f"{r['name']} (not budgeted)", "", _cents(r["actual_total"]), ""])
    rs = s["reserve"]
    rows += [[], ["Reserve fund activity"]]
    for label, key in (
        ("Opening balance", "opening"),
        ("Transfers in", "transfers_in"),
        ("Interest and other income", "income"),
        ("Spent from reserves", "expenses"),
        ("Between funds", "interfund"),
        ("Distributions and other", "other"),
        ("Closing balance", "closing"),
    ):
        rows.append([label, _cents(rs[key])])
    rows += [[], ["Unpaid balances by age", f"at {p['end']:%Y-%m-%d}"]]
    for label, cents in s["aging"]["rows"]:
        rows.append([label, _cents(cents)])
    if s["annual"]:
        a = s["annual"]
        rows += [
            [],
            ["Annual accounting (operating fund)"],
            ["Income", _cents(a["income_cents"])],
            ["Expenditures", _cents(a["expenditures_cents"])],
            ["Reserves set aside", _cents(a["reserves_cents"])],
            [
                "Net excess (deficit) of income over expenditures plus reserves",
                _cents(a["net_cents"]),
            ],
        ]
    if s["tax"]:
        t = s["tax"]
        rows += [
            [],
            ["Tax worksheet"],
            ["Exempt-function income", _cents(t["exempt_function_cents"])],
            *[[f"Other income: {a}", _cents(c)] for a, c in t["other_income_rows"]],
            ["Other income", _cents(t["other_income_cents"])],
            ["Gross income", _cents(t["gross_income_cents"])],
            [
                "Exempt share",
                f"{t['exempt_share_percent']}%" if t["exempt_share_percent"] is not None else "",
            ],
            [t["note"]],
        ]
    return rows


def statements_csv(s: dict, *, actor, request=None) -> bytes:
    require(actor, "statements.view")
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in csv_rows(s):
        writer.writerow([safe_cell(c) if not _numeric(c) else c for c in row])
    _audit("csv", s, actor, request)
    return buf.getvalue().encode()


def _numeric(value: str) -> bool:
    v = value[1:] if value.startswith("-") else value
    return v.replace(".", "", 1).isdigit()


def _row(label: str, *values: str, width: int = 44) -> str:
    return pdf.plain(label)[:width].ljust(width) + "".join(v.rjust(14) for v in values)


def pdf_paragraphs(s: dict) -> list[tuple[str, str]]:
    from community.models import CommunityProfile

    p = s["period"]
    out: list[tuple[str, str]] = [
        ("small", pdf.plain(CommunityProfile.get().legal_name)),
        ("heading", f"Financial statements: {p['label']}"),
        ("normal", "Closed" if p["closed"] else "NOT CLOSED: figures may still change."),
        ("small", s["basis"]),
        ("normal", ""),
        ("subheading", f"Balance sheet at {p['end']:%B %-d, %Y}"),
    ]
    for _fund, f in s["balance_sheet"]["funds"].items():
        out.append(("rowhead", _row(f["label"], "")))
        for account, cents in f["assets_rows"]:
            out.append(("row", _row(f"  {account}", format_cents(cents))))
        for account, cents in f["liabilities_rows"]:
            out.append(("row", _row(f"  Less {account}", format_cents(cents))))
        out.append(("row", _row("  Fund balance", format_cents(f["fund_balance"]))))
    bs = s["balance_sheet"]
    out.append(("rowhead", _row("All funds", format_cents(bs["total_fund_balance"]))))
    inc = s["income"]
    out += [("normal", ""), ("subheading", "Income and expenses")]
    out.append(("rowhead", _row("", "Period", "Year to date")))
    ytd = {
        r["account"]: r["total"]
        for r in s["year_to_date"]["income_rows"] + s["year_to_date"]["expense_rows"]
    }
    for label, key in (("Income", "income_rows"), ("Expenses", "expense_rows")):
        out.append(("rowhead", _row(label, "")))
        for r in inc[key]:
            out.append(
                (
                    "row",
                    _row(
                        f"  {r['account']}",
                        format_cents(r["total"]),
                        format_cents(ytd.get(r["account"], 0)),
                    ),
                )
            )
    out.append(
        (
            "rowhead",
            _row(
                "Net income",
                format_cents(inc["net_total"]),
                format_cents(s["year_to_date"]["net_total"]),
            ),
        )
    )
    if inc["transfers_total"]:
        out.append(
            ("row", _row("Transfers between funds (net)", format_cents(inc["transfers_total"])))
        )
    b = s["budget"]
    out += [
        ("normal", ""),
        ("subheading", f"Budget against actual, fiscal year {b['fiscal_year']}"),
    ]
    if b["budget"] is None:
        out.append(("small", "No adopted budget for this year: actual figures only."))
    out.append(("rowhead", _row("", "Budget", "Actual", "Variance")))
    for r in b["rows"]:
        out.append(
            (
                "row",
                _row(
                    r["name"],
                    format_cents(r["planned_total"]),
                    format_cents(r["actual_total"]),
                    format_cents(r["variance"]),
                ),
            )
        )
    for r in b["unbudgeted"]:
        out.append(
            ("row", _row(f"{r['name']} (not budgeted)", "", format_cents(r["actual_total"])))
        )
    rs = s["reserve"]
    out += [("normal", ""), ("subheading", "Reserve fund activity")]
    for label, key in (
        ("Opening balance", "opening"),
        ("Transfers in", "transfers_in"),
        ("Interest and other income", "income"),
        ("Spent from reserves", "expenses"),
        ("Between funds", "interfund"),
        ("Distributions and other", "other"),
        ("Closing balance", "closing"),
    ):
        out.append(("row", _row(label, format_cents(rs[key]))))
    ag = s["aging"]
    out += [("normal", ""), ("subheading", "Unpaid balances by age")]
    for label, cents in ag["rows"]:
        out.append(("row", _row(label, format_cents(cents))))
    out.append(("row", _row(f"Homes with a balance due: {ag['homes']}", format_cents(ag["total"]))))
    if s["annual"]:
        a = s["annual"]
        out += [
            ("normal", ""),
            ("subheading", "Annual accounting"),
            ("row", _row("Income", format_cents(a["income_cents"]))),
            ("row", _row("Expenditures", format_cents(a["expenditures_cents"]))),
            ("row", _row("Reserves set aside", format_cents(a["reserves_cents"]))),
            ("rowhead", _row("Net excess (deficit)", format_cents(a["net_cents"]))),
            (
                "small",
                "The net excess or deficit of income over expenditures plus reserves. "
                "Any surplus distribution is the board's decision (Declaration; "
                "68 Pa.C.S. § 5313).",
            ),
        ]
    if s["tax"]:
        t = s["tax"]
        out += [
            ("normal", ""),
            ("subheading", "Tax worksheet for the preparer"),
            ("row", _row("Exempt-function income", format_cents(t["exempt_function_cents"]))),
            ("row", _row("Other income", format_cents(t["other_income_cents"]))),
            ("row", _row("Gross income", format_cents(t["gross_income_cents"]))),
            ("small", t["note"]),
        ]
    return out


def statements_pdf(s: dict, *, actor, request=None) -> bytes:
    require(actor, "statements.view")
    data = pdf.render(pdf_paragraphs(s), title=f"Financial statements {s['period']['label']}")
    _audit("pdf", s, actor, request)
    return data


def _audit(fmt: str, s: dict, actor, request) -> None:
    record_event(
        "books.statements_exported",
        actor=_user(actor),
        request=request,
        metadata={"period": s["period"]["key"], "format": fmt, "closed": s["period"]["closed"]},
    )


def owner_annual_accounting() -> list[dict]:
    """For owners (Budget and reserves): the annual accounting of each closed
    fiscal year, the figures §8.8 has owners receive. Nothing per home."""
    out = []
    for key in sorted(periods.standing_closes(PeriodKind.YEAR), reverse=True)[:3]:
        out.append(annual_accounting(int(key[2:])))
    return out

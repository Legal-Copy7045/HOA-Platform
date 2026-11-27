"""Books and close (D69-D71): funds and bank accounts, coding bank lines and
rules, the automatic month close and each of its exceptions, the period lock
(services and the database trigger), the two-person reopen, the year close,
the statements' arithmetic, the year-end package and its signed manifest,
the tax worksheet and 1099 files, authorisation and the audit metadata."""

from __future__ import annotations

import datetime
import io
import json
import zipfile

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, InternalError, ProgrammingError, transaction
from django.urls import reverse

from audit.models import AuditEvent
from community.models import CommunityProfile, Role
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from ledger import services as ledger
from ledger.books import (
    close,
    coding,
    funds,
    journal,
    package,
    periods,
    quickbooks,
    statements,
    tax,
)
from ledger.books.funds import BooksError
from ledger.books.models import (
    CloseRun,
    ExceptionNote,
    Fund,
    GLCategory,
    GLEntry,
    LineSign,
    PeriodClose,
    YearEndPackage,
)
from ledger.models import AccountingSetup, BankConnection, BankTransaction, LedgerEntry

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 9, 30)
OCT = "2026-10"
OCT_END = datetime.date(2026, 10, 31)
RUN_DAY = datetime.date(2026, 11, 5)


_SEQ = [0]


def _line(conn, day, cents, text):
    _SEQ[0] += 1
    n = _SEQ
    return BankTransaction.objects.create(
        connection=conn,
        bank_id=f"t{n[0]}",
        posted_on=day,
        amount_cents=cents,
        description=text,
        source="csv",
    )


def _balance(conn, day, cents):
    from ledger.models import BankBalance

    BankBalance.objects.create(connection=conn, as_of=day, balance_cents=cents, source="entered")


def _cat(key):
    return GLCategory.objects.get(key=key)


@pytest.fixture
def w(settings):
    funds.ensure_default_categories()
    board = make_member(board=True)
    board2 = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])
    auditor = make_member(roles=[Role.AUDITOR])
    lot = LotFactory(street_address="1 Alpha Way")
    lot_b = LotFactory(street_address="2 Bravo Way")
    owner = make_member(lots=[lot])
    setup = AccountingSetup.get()
    setup.opening_cash_on = START
    setup.opening_cash_cents = 100000
    setup.save()
    op = BankConnection.objects.create(provider="file", display_name="Operating", created_by=board)
    rs = BankConnection.objects.create(provider="file", display_name="Reserve", created_by=board)
    funds.save_account(
        op, fund=Fund.OPERATING, is_primary=True, opening_balance_cents=100000, actor=SYSTEM
    )
    funds.save_account(rs, fund=Fund.RESERVE, opening_balance_cents=500000, actor=SYSTEM)
    return {
        "board": board,
        "board2": board2,
        "manager": manager,
        "auditor": auditor,
        "owner": owner,
        "lot": lot,
        "lot_b": lot_b,
        "op": op,
        "rs": rs,
    }


def _rules(w):
    for text, sign, key in (
        ("INTEREST", LineSign.IN, "interest_income"),
        ("SERVICE CHARGE", LineSign.OUT, "bank_fees"),
        ("TRANSFER TO SAVINGS", LineSign.OUT, "transfer"),
        ("TRANSFER FROM CHECKING", LineSign.IN, "transfer"),
    ):
        coding.save_rule(
            name=text,
            contains=text,
            sign=sign,
            category=_cat(key),
            actor=w["board"],
            apply_now=False,
        )


def _october(w, *, balances=True):
    _line(w["rs"], datetime.date(2026, 10, 5), 125, "INTEREST PAYMENT")
    _line(w["op"], datetime.date(2026, 10, 6), -1500, "MONTHLY SERVICE CHARGE")
    _line(w["op"], datetime.date(2026, 10, 15), -50000, "TRANSFER TO SAVINGS ••••1234")
    _line(w["rs"], datetime.date(2026, 10, 15), 50000, "TRANSFER FROM CHECKING ••••9876")
    if balances:
        _balance(w["op"], OCT_END, 100000 - 1500 - 50000)
        _balance(w["rs"], OCT_END, 500000 + 125 + 50000)


def _check(ev, key):
    return next(c for c in ev.checks if c.key == key)


# --- Funds and coding ---------------------------------------------------------------------------


def test_rules_code_new_lines_and_record_who(w):
    _rules(w)
    _october(w)
    assert coding.apply_rules() == 4
    entries = list(GLEntry.objects.order_by("id"))
    assert {e.rule_id for e in entries} == set(
        GLEntry.objects.values_list("rule_id", flat=True)
    ) and all(e.rule_id and e.coded_by_id is None for e in entries)
    interest = GLEntry.objects.get(category__key="interest_income")
    assert interest.fund == Fund.RESERVE and interest.cash_fund == Fund.RESERVE
    assert coding.unhandled_lines() == []


def test_manual_coding_then_rule_suggestion(w):
    for day in (3, 10, 17):
        line = _line(w["op"], datetime.date(2026, 10, day), -2000, f"PECO ENERGY {day}9912")
        coding.code_line(line, _cat("utilities"), actor=w["manager"])
    sugg = coding.rule_suggestions()
    assert sugg and sugg[0]["contains"] == "PECO ENERGY" and sugg[0]["count"] == 3
    assert GLEntry.objects.filter(coded_by=w["manager"]).count() == 3


def test_manager_codes_but_never_makes_rules_or_closes(w):
    with pytest.raises(PermissionDenied):
        coding.save_rule(
            name="x", contains="INTEREST", category=_cat("interest_income"), actor=w["manager"]
        )
    with pytest.raises(PermissionDenied):
        close.close_month(OCT, actor=w["manager"])


def test_uncode_posts_a_reversal(w):
    line = _line(w["op"], datetime.date(2026, 10, 6), -1500, "SERVICE CHARGE")
    entry = coding.code_line(line, _cat("bank_fees"), actor=w["board"])
    with pytest.raises(BooksError):
        coding.code_line(line, _cat("bank_fees"), actor=w["board"])
    rev = coding.uncode(entry, reason="wrong category", actor=w["board"])
    assert rev.amount_cents == 1500 and rev.reverses_id == entry.pk
    assert line in coding.unhandled_lines()


def test_save_account_refuses_reserve_primary(w):
    with pytest.raises(BooksError):
        funds.save_account(
            w["rs"], fund=Fund.RESERVE, is_primary=True, opening_balance_cents=0, actor=w["board"]
        )


# --- The automatic month close -----------------------------------------------------------------


def test_auto_close_happy_path(w):
    _rules(w)
    _october(w)
    result = close.run(today=RUN_DAY)
    assert result["coded"] == 4
    assert result["closed"] == [OCT]
    pc = PeriodClose.objects.get(period=OCT)
    assert pc.how == "auto" and pc.closed_by is None
    assert all(a["difference"] == 0 for a in pc.summary["accounts"])
    assert periods.is_closed(datetime.date(2026, 10, 10))
    assert AuditEvent.objects.filter(action="books.period_closed").exists()


def test_not_before_the_close_day(w):
    _rules(w)
    _october(w)
    assert close.run(today=datetime.date(2026, 11, 2))["closed"] == []


def test_auto_close_off_leaves_it_to_the_board(w):
    from community import services as community

    community.set_rule(key="books.auto_close", value="false", citation="t", actor=SYSTEM)
    _rules(w)
    _october(w)
    assert close.run(today=RUN_DAY)["closed"] == []
    assert close.evaluate_month(OCT).closable
    close.close_month(OCT, actor=w["board"])
    assert PeriodClose.objects.get(period=OCT).how == "manual"


def test_uncoded_line_is_an_exception_and_a_my_item(w):
    _october(w)  # no rules
    close.run(today=RUN_DAY)
    assert not PeriodClose.objects.exists()
    run = CloseRun.objects.get(period=OCT)
    assert not run.passed and run.blocking >= 4
    assert "description" not in json.dumps(run.results)
    from core.my_items import items_for

    titles = [i.title for i in items_for(w["board"])]
    assert any("October 2026 books" in t for t in titles)
    # Resolve from the Close page.
    for line in coding.unhandled_lines():
        key = {"INT": "interest_income", "MON": "bank_fees"}.get(line.description[:3], "transfer")
        coding.code_line(line, _cat(key), actor=w["board"])
    assert close.run(today=RUN_DAY)["closed"] == [OCT]
    assert not any("October 2026 books" in i.title for i in items_for(w["board"]))


def test_missing_bank_balance_is_an_exception(w):
    _rules(w)
    _october(w, balances=False)
    ev = close.evaluate_month(OCT)
    assert len(_check(ev, "bank_balance").open_items) == 2
    close.enter_balance(w["op"], as_of=OCT_END, balance_cents=48500, actor=w["manager"])
    close.enter_balance(w["rs"], as_of=OCT_END, balance_cents=550125, actor=w["manager"])
    coding.apply_rules()
    assert close.evaluate_month(OCT).passed


def test_difference_needs_a_second_board_member(w):
    _rules(w)
    _october(w)
    from ledger.models import BankBalance

    BankBalance.objects.create(
        connection=w["op"], as_of=OCT_END, balance_cents=48400, source="entered"
    )
    coding.apply_rules()
    ev = close.evaluate_month(OCT)
    rec = _check(ev, "reconciliation")
    assert rec.open_items and rec.open_items[0].amount_cents == -100
    item = rec.open_items[0]
    note = close.record_note(
        period=OCT,
        check_key="reconciliation",
        item=item.key,
        amount_cents=-100,
        reason="Bank error, corrected in November",
        actor=w["board"],
    )
    assert not close.evaluate_month(OCT).passed
    with pytest.raises(BooksError):
        close.agree(note, actor=w["board"])
    with pytest.raises(PermissionDenied):
        close.agree(note, actor=w["manager"])
    close.agree(note, actor=w["board2"])
    assert close.evaluate_month(OCT).passed


def test_unpaired_transfer_blocks(w):
    _rules(w)
    _line(w["op"], datetime.date(2026, 10, 15), -50000, "TRANSFER TO SAVINGS")
    _balance(w["op"], OCT_END, 50000)
    _balance(w["rs"], OCT_END, 500000)
    coding.apply_rules()
    ev = close.evaluate_month(OCT)
    assert _check(ev, "transfers").open_items
    assert not ev.passed


def test_undeposited_payment_blocks(w):
    ledger.record_payment(
        lot=w["lot"],
        amount_cents=15000,
        received_on=datetime.date(2026, 10, 2),
        reference="101",
        actor=SYSTEM,
    )
    _balance(w["op"], OCT_END, 100000)
    _balance(w["rs"], OCT_END, 500000)
    ev = close.evaluate_month(OCT)
    assert _check(ev, "undeposited").open_items
    from ledger import accounting

    accounting.create_deposit(
        entry_ids=[e.pk for e in accounting.undeposited_payments()],
        deposited_on=datetime.date(2026, 10, 3),
        actor=w["board"],
    )
    ev = close.evaluate_month(OCT)
    assert not _check(ev, "undeposited").open_items
    # The deposit is in the books but not on the bank: in transit, no difference.
    rec = next(a for a in ev.accounts if a.setup.connection_id == w["op"].pk)
    assert rec.outstanding_in == 15000 and rec.difference == 0


def test_negative_fund_blocks_and_previous_month_must_close_first(w):
    _line(w["rs"], datetime.date(2026, 10, 20), -600000, "WITHDRAWAL")
    coding.code_line(
        BankTransaction.objects.get(description="WITHDRAWAL"), _cat("adjustments"), actor=w["board"]
    )
    _balance(w["op"], OCT_END, 100000)
    _balance(w["rs"], OCT_END, -100000)
    ev = close.evaluate_month(OCT)
    assert _check(ev, "funds").open_items
    nov = close.evaluate_month("2026-11")
    assert _check(nov, "previous").open_items


def test_late_fees_posted_or_waived(w, settings):
    from community import services as community

    settings.LEDGER_LATE_FEES_ENABLED = True
    for key, value in (
        ("late_fee.cents", "2500"),
        ("late_fee.grace_days", "10"),
        ("dues.monthly_cents", "15000"),
    ):
        community.set_rule(key=key, value=value, citation="t", actor=SYSTEM)
    ledger.post_monthly_assessments(year=2026, month=10, actor=SYSTEM)
    _balance(w["op"], OCT_END, 100000)
    _balance(w["rs"], OCT_END, 500000)
    ev = close.evaluate_month(OCT)
    late = _check(ev, "late_fees")
    assert len(late.open_items) == 2
    assert not _check(ev, "assessments").open_items
    close.record_note(
        period=OCT,
        check_key="late_fees",
        item=f"lot:{w['lot_b'].pk}",
        reason="Hardship waiver, board decision",
        actor=w["board"],
    )
    with pytest.raises(PermissionDenied):
        close.record_note(
            period=OCT,
            check_key="late_fees",
            item=f"lot:{w['lot'].pk}",
            reason="Manager cannot waive",
            actor=w["manager"],
        )
    late = _check(close.evaluate_month(OCT), "late_fees")
    assert len(late.open_items) == 1


def test_own_home_late_fee_cannot_be_waived_by_its_owner(w):
    from community.tests.factories import PersonFactory  # noqa: F401

    owner_board = make_member(board=True, lots=[w["lot_b"]])
    with pytest.raises(BooksError):
        close.record_note(
            period=OCT,
            check_key="late_fees",
            item=f"lot:{w['lot_b'].pk}",
            reason="My own home",
            actor=owner_board,
        )


def test_reserve_contribution_and_stale_checks_are_warnings(w):
    from community import services as community

    community.set_rule(key="books.reserve_monthly_cents", value="60000", citation="t", actor=SYSTEM)
    _rules(w)
    _october(w)
    coding.apply_rules()
    ev = close.evaluate_month(OCT)
    reserve = _check(ev, "reserve_contribution")
    assert reserve.open_items and reserve.status == "warn"
    assert ev.passed  # warnings never block


# --- The lock ------------------------------------------------------------------------------------


def _close_october(w):
    _rules(w)
    _october(w)
    close.run(today=RUN_DAY)
    assert periods.is_closed(datetime.date(2026, 10, 1))


def test_services_refuse_a_closed_month(w):
    _close_october(w)
    with pytest.raises(ledger.LedgerError):
        ledger.record_payment(
            lot=w["lot"],
            amount_cents=100,
            received_on=datetime.date(2026, 10, 10),
            reference="9",
            actor=SYSTEM,
        )
    errors = ledger.check_payment(
        ledger.BatchPayment(w["lot"], 100, datetime.date(2026, 10, 10), "8")
    )
    assert any("closed" in e for e in errors)
    from ledger import accounting

    with pytest.raises(accounting.AccountingError):
        accounting.create_deposit(
            entry_ids=[1], deposited_on=datetime.date(2026, 10, 9), actor=w["board"]
        )


def test_trigger_refuses_rows_in_a_closed_month(w):
    _close_october(w)
    with pytest.raises((IntegrityError, InternalError, ProgrammingError)), transaction.atomic():
        LedgerEntry.objects.create(
            lot=w["lot"],
            posted_on=datetime.date(2026, 10, 9),
            kind="credit",
            amount_cents=-100,
            description="backdated",
        )
    with pytest.raises((IntegrityError, InternalError, ProgrammingError)), transaction.atomic():
        line = BankTransaction.objects.first()
        GLEntry.objects.create(
            transaction=line,
            posted_on=datetime.date(2026, 10, 9),
            category=_cat("bank_fees"),
            fund="operating",
            cash_fund="operating",
            cash_account="x",
            category_account="y",
            amount_cents=-1,
        )


def test_corrections_post_in_the_open_month(w):
    _close_october(w)
    entry = GLEntry.objects.get(category__key="bank_fees")
    rev = coding.uncode(entry, reason="really insurance", actor=w["board"])
    assert rev.posted_on > OCT_END and "Correction" in rev.reference
    redo = coding.code_line(entry.transaction, _cat("insurance"), actor=w["board"])
    assert redo.posted_on > OCT_END and "2026-10-06" in redo.reference


def test_reopen_needs_two_board_members(w):
    _close_october(w)
    pc = PeriodClose.objects.get(period=OCT)
    with pytest.raises(PermissionDenied):
        periods.request_reopen(pc, reason="Missing a check", actor=w["manager"])
    req = periods.request_reopen(pc, reason="A check was missed", actor=w["board"])
    with pytest.raises(periods.PeriodError):
        periods.decide_reopen(req, approve=True, actor=w["board"])
    periods.decide_reopen(req, approve=True, actor=w["board2"])
    assert not periods.is_closed(datetime.date(2026, 10, 10))
    ledger.record_payment(
        lot=w["lot"],
        amount_cents=100,
        received_on=datetime.date(2026, 10, 10),
        reference="9",
        actor=SYSTEM,
    )
    assert AuditEvent.objects.filter(action="books.reopen_approved").exists()


# --- Statements --------------------------------------------------------------------------------


def test_balance_sheet_balances_by_fund(w):
    _close_october(w)
    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=4000,
        description="Key fob",
        posted_on=datetime.date(2026, 11, 2),
        actor=SYSTEM,
    )
    bs = statements.balance_sheet(OCT_END)
    op, rs = bs["funds"]["operating"], bs["funds"]["reserve"]
    assert op["fund_balance"] == 48500 and rs["fund_balance"] == 550125
    for f in bs["funds"].values():
        assert f["assets"] - f["liabilities"] == f["fund_balance"]
    inc = statements.income_statement(datetime.date(2026, 10, 1), OCT_END)
    assert inc["income_total"] == 125 and inc["expense_total"] == 1500
    ra = statements.reserve_activity(datetime.date(2026, 10, 1), OCT_END)
    assert ra["transfers_in"] == 50000 and ra["income"] == 125 and ra["adds_up"]
    later = statements.balance_sheet(datetime.date(2026, 11, 5))
    assert later["funds"]["operating"]["fund_balance"] == 48500 + 4000
    assert later["homes_owing"] == 1


def test_annual_accounting_figure(w):
    _close_october(w)
    a = statements.annual_accounting(2026)
    assert a["reserves_cents"] == 50000
    assert a["net_cents"] == a["income_cents"] - a["expenditures_cents"] - 50000


def test_journal_export_keeps_its_layout_and_adds_bank_lines(w):
    _close_october(w)
    from ledger.accounting import EXPORT_HEADER, journal_rows

    rows = journal_rows(datetime.date(2026, 10, 1), OCT_END)
    assert all(len(r) == len(EXPORT_HEADER) for r in rows)
    bank = [r for r in rows if r[1] == "Bank"]
    assert len(bank) == 8
    assert sum(float(r[4] or 0) for r in rows) == sum(float(r[5] or 0) for r in rows)
    reserve_cash = [r for r in bank if r[3].startswith("1010")]
    assert reserve_cash
    qbo = quickbooks.rows(journal.journal(datetime.date(2026, 10, 1), OCT_END))
    assert qbo[0][-1] in ("Operating fund", "Reserve fund") and all(r[6] == "" for r in qbo)


def test_statement_downloads_and_permissions(client, w):
    _close_october(w)
    client.force_login(w["auditor"])
    page = client.get(reverse("financial_statements") + "?month=2026-10")
    assert page.status_code == 200 and b"Fund balance" in page.content
    assert client.get(reverse("financial_statements_pdf") + "?year=2026").status_code == 200
    csv_ = client.get(reverse("financial_statements_csv") + "?month=2026-10")
    assert b"Balance sheet" in csv_.content
    client.force_login(w["owner"])
    for name in (
        "financial_statements",
        "books_close",
        "books_accounts",
        "books_coding",
        "tax_worksheet",
        "year_end_package",
        "financial_statements_csv",
    ):
        assert client.get(reverse(name)).status_code in (403, 404), name


def test_board_pages_load(client, w):
    _rules(w)
    _october(w)
    client.force_login(w["board"])
    for name in (
        "books_close",
        "books_accounts",
        "books_coding",
        "financial_statements",
        "tax_worksheet",
    ):
        assert client.get(reverse(name)).status_code == 200, name
    page = client.get(reverse("books_close") + "?period=2026-10").content.decode()
    assert "Every bank line is matched or coded" in page
    r = client.post(reverse("books_action"), {"action": "run", "period": OCT})
    assert r.status_code == 302 and PeriodClose.objects.filter(period=OCT).exists()


# --- The year ------------------------------------------------------------------------------------


@pytest.fixture
def fy(w):
    """Fiscal years from November: FY2025 is Nov 2025 to Oct 2026."""
    profile = CommunityProfile.get()
    profile.fiscal_year_start_month = 11
    profile.save()
    setup = AccountingSetup.get()
    setup.opening_cash_on = datetime.date(2025, 10, 31)
    setup.save()
    day = datetime.date(2025, 11, 30)
    while day < OCT_END:
        _balance(w["op"], day, 100000)
        _balance(w["rs"], day, 500000)
        day = periods.month_bounds(periods.month_key(periods.next_month(day)))[1]
    _rules(w)
    _october(w)
    # Member income, so the association qualifies for Form 1120-H.
    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=100000,
        description="Clubhouse fee",
        posted_on=datetime.date(2026, 10, 10),
        actor=SYSTEM,
    )
    return w


def test_year_closes_by_itself_and_builds_the_package(fy, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        result = close.run(today=RUN_DAY)
    assert len([k for k in result["closed"] if not k.startswith("FY")]) == 12
    assert "FY2025" in result["closed"]
    pkg = YearEndPackage.objects.get(fiscal_year=2025)
    assert pkg.automatic and pkg.year_closed
    data = pkg.file.open("rb").read()
    assert package.verify(data) == []
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    for expected in (
        "statements/financial-statements.pdf",
        "journal/journal.csv",
        "journal/quickbooks-online-journal.csv",
        "reconciliations/month-end-reconciliations.csv",
        "owners/owner-subledger.csv",
        "approvals/approval-trail.csv",
        "handover/treasurer-handover.pdf",
        "tax/form-1120-h-worksheet.csv",
        "tax/1099-iris-upload-2026.csv",
        "manifest.json",
        "README.txt",
    ):
        assert expected in names, expected
    # Deadlines were put on Recurring deadlines.
    from operations.models import ComplianceItem

    assert ComplianceItem.objects.filter(title__startswith="Federal Form 1120-H").exists()


def test_package_tampering_is_detected(fy, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        close.run(today=RUN_DAY)
    data = YearEndPackage.objects.get(fiscal_year=2025).file.open("rb").read()
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for name in src.namelist():
            body = src.read(name)
            if name == "journal/journal.csv":
                body += b"tampered\r\n"
            out.writestr(name, body)
    problems = package.verify(buf.getvalue())
    assert any("journal/journal.csv" in p for p in problems)
    assert package.verify(data, key=b"another key") == ["The manifest's signature does not match."]


def test_package_download_is_audited_and_board_auditor_only(client, fy):
    pkg = package.build(2025, actor=fy["board"])
    client.force_login(fy["manager"])
    assert client.get(reverse("year_end_package") + f"?id={pkg.pk}").status_code == 403
    client.force_login(fy["auditor"])
    r = client.get(reverse("year_end_package") + f"?id={pkg.pk}")
    assert r.status_code == 200
    assert b"".join(r.streaming_content)[:2] == b"PK"
    ev = AuditEvent.objects.filter(action="books.package_downloaded").get()
    assert set(ev.metadata) <= {"fiscal_year", "files", "size"}


def test_year_waits_for_its_months(fy):
    ev = close.evaluate_year(2025)
    assert _check(ev, "months").open_items


# --- Tax ----------------------------------------------------------------------------------------


def test_1120h_worksheet_arithmetic(fy):
    close.run(today=RUN_DAY)
    from community import services as community

    community.set_rule(key="dues.monthly_cents", value="15000", citation="t", actor=SYSTEM)
    w = tax.form_1120h(2025)
    lines = {n: c for n, _l, c in w["lines"]}
    assert lines["2"] == 125  # bank interest
    assert lines["18"] == 125  # specific deduction capped by income
    assert lines["19"] == 0 and lines["20"] == 0


def test_1120h_tax_and_tests(w):
    from community import services as community

    _rules(w)
    big = _line(w["rs"], datetime.date(2026, 10, 5), 1000000, "INTEREST ON CD")
    coding.code_line(
        big, _cat("interest_income"), actor=w["board"]
    ) if big in coding.unhandled_lines() else None
    coding.apply_rules()
    wks = tax.form_1120h(2026)
    lines = {n: c for n, _l, c in wks["lines"]}
    assert lines["2"] == 1000000
    assert lines["19"] == 1000000 - 10000
    assert lines["20"] == (1000000 - 10000) * 30 // 100
    assert not wks["sixty"]["passes"]  # all income is interest
    community.set_rule(key="tax.hoa_rate_bp", value="3200", citation="t", actor=SYSTEM)
    assert tax.form_1120h(2026)["tax_cents"] == (1000000 - 10000) * 32 // 100
    # Spending on penalties fails the 90% test.
    pen = _line(w["op"], datetime.date(2026, 10, 7), -5000, "IRS PENALTY")
    coding.code_line(pen, _cat("penalties_paid"), actor=w["board"])
    assert not tax.form_1120h(2026)["ninety"]["passes"]


def test_failed_test_is_a_year_close_exception(fy):
    big = _line(fy["rs"], datetime.date(2026, 10, 20), 900000, "INTEREST ON CD")
    from ledger.models import BankBalance

    BankBalance.objects.create(
        connection=fy["rs"],
        as_of=OCT_END,
        balance_cents=500000 + 125 + 50000 + 900000,
        source="entered",
    )
    close.run(today=RUN_DAY)
    assert big.pk not in {line.pk for line in coding.unhandled_lines()}
    ev = close.evaluate_year(2025)
    item = _check(ev, "tax_tests").open_items[0]
    assert "Form 1120 may be needed" in item.title
    assert not YearEndPackage.objects.exists()


def test_w9_missing_blocks_the_year(fy):
    from operations.models import ExpenseCategory, Invoice, InvoicePayment, Vendor

    vendor = Vendor.objects.create(name="Mow Co (demo)", address="9 Field Rd")
    cat = ExpenseCategory.objects.create(code="5200", name="Landscaping")
    inv = Invoice.objects.create(
        vendor=vendor,
        number="A1",
        invoice_date=datetime.date(2026, 3, 1),
        amount_cents=300000,
        category=cat,
        state="paid",
        entered_by=fy["board"],
    )
    InvoicePayment.objects.create(
        invoice=inv,
        paid_on=datetime.date(2026, 3, 5),
        method="check",
        reference="77",
        amount_cents=300000,
        recorded_by=fy["board"],
    )
    ev = close.evaluate_year(2025)
    assert _check(ev, "vendor_1099").open_items
    rows = tax.iris_rows(2026)
    assert rows and rows[0][0] == "1099-NEC" and rows[0][3] == "" and rows[0][5] == ""
    assert rows[0][9] == "3000.00"
    assert tax.IRIS_HEADER[5] == "Recipient TIN"
    copies = " ".join(text for _style, text in tax.recipient_paragraphs(2026))
    assert "not kept by the portal" in copies and "9 Field Rd" in copies


def test_tax_downloads_board_and_auditor_only(client, w):
    client.force_login(w["manager"])
    assert client.get(reverse("tax_worksheet")).status_code == 403
    client.force_login(w["auditor"])
    assert client.get(reverse("tax_worksheet") + "?year=2026").status_code == 200
    r = client.get(reverse("tax_download") + "?year=2026&format=iris")
    assert r.content.decode().splitlines()[0].startswith("Form Type,Tax Year")
    ev = AuditEvent.objects.filter(action="tax.exported").get()
    assert set(ev.metadata) == {"year", "format", "count"}


def test_deadlines_are_created_once(w):
    from operations.models import ComplianceItem

    assert tax.ensure_deadlines(actor=w["board"], fiscal_year=2026) == 3
    assert tax.ensure_deadlines(actor=w["board"], fiscal_year=2026) == 0
    pa = ComplianceItem.objects.get(category="corporate")
    assert "15 Pa.C.S." in pa.citation and pa.recurrence == "annual"
    assert ComplianceItem.objects.get(title__startswith="Federal").due_on == datetime.date(
        2027, 4, 15
    )


# --- Audit ---------------------------------------------------------------------------------------


def test_audit_metadata_has_no_bank_text_or_names(w):
    _rules(w)
    _october(w)
    close.run(today=RUN_DAY)
    for ev in AuditEvent.objects.filter(action__startswith="books."):
        text = json.dumps(ev.metadata)
        assert "INTEREST" not in text and "TRANSFER" not in text and "Alpha" not in text
    assert ExceptionNote.objects.count() == 0


# --- Demo data -----------------------------------------------------------------------------------


def test_demo_books_seed():
    from ledger.demo_books import seed

    make_member(board=True, lots=[LotFactory(street_address="5 Demo Lane")])
    result = seed(today=datetime.date(2026, 11, 20))
    assert result["accounts"] == 2 and result["closed_months"] >= 12
    assert PeriodClose.objects.filter(kind="year", period="FY2025").exists()
    assert YearEndPackage.objects.filter(fiscal_year=2025).exists()
    assert not CloseRun.objects.get(period="2026-10").passed
    assert seed(today=datetime.date(2026, 11, 20)) == {"skipped": True}

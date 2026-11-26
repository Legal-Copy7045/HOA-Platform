"""Milestone 4 part 3: accounting export, deposits, reconciliation, 1099,
courtesy reminders and the collections letter sequence."""

import csv
import datetime
import io
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.scoping import system_access
from ledger import accounting as acc
from ledger import services
from ledger.models import CollectionLetter, DepositBatch, EntryKind
from operations.models import ExpenseCategory, Invoice, InvoicePayment, Vendor

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = timezone.localdate()


@pytest.fixture
def w():
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2 (demo)", actor=board
    )
    lot = LotFactory()
    owner = make_member(lots=[lot])
    manager = make_member(roles=[Role.MANAGER])
    vendor = Vendor.objects.create(name="=Mow Co", w9_on_file=False)
    category = ExpenseCategory.objects.create(code="5100", name="Landscaping")
    invoice = Invoice.objects.create(
        vendor=vendor,
        number="A1",
        invoice_date=TODAY,
        amount_cents=250000,
        category=category,
        entered_by=manager,
        state="paid",
    )
    return {
        "board": board,
        "lot": lot,
        "owner": owner,
        "manager": manager,
        "vendor": vendor,
        "invoice": invoice,
    }


def _pay_vendor(w, cents, on=TODAY):
    return InvoicePayment.objects.create(
        invoice=w["invoice"], paid_on=on, method="check", amount_cents=cents, recorded_by=w["board"]
    )


def _owner_payment(w, cents, on=TODAY):
    return services.record_payment(
        lot=w["lot"], amount_cents=cents, received_on=on, actor=w["manager"]
    )


# --- Export ---------------------------------------------------------------------------


def test_journal_balances_and_never_names_owners(w):
    services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=1000,
        description="Key",
        actor=w["board"],
    )
    _owner_payment(w, 1000)
    _pay_vendor(w, 250000)
    data = acc.export_csv(TODAY - datetime.timedelta(days=1), TODAY, actor=w["manager"])
    rows = list(csv.reader(io.StringIO(data.decode())))
    assert rows[0] == acc.EXPORT_HEADER
    debit = sum(int(float(r[4]) * 100) for r in rows[1:] if r[4])
    credit = sum(int(float(r[5]) * 100) for r in rows[1:] if r[5])
    assert debit == credit == 1000 + 1000 + 250000
    text = data.decode()
    with system_access("t"):
        from community.models import Person

        owner_name = Person.objects.get(user=w["owner"]).display_name
    assert owner_name not in text and w["owner"].email not in text
    assert "'=Mow Co" in text  # formula injection neutralised
    assert "5100 Landscaping" in text and "1000 Operating cash" in text
    assert AuditEvent.objects.filter(action="accounting.exported").exists()


def test_owners_cannot_export(client, w):
    with pytest.raises(PermissionDenied):
        acc.export_csv(TODAY, TODAY, actor=w["owner"])
    client.force_login(w["owner"])
    url = reverse("accounting_export") + f"?start={TODAY}&end={TODAY}"
    assert client.get(url).status_code == 403


# --- Deposits ----------------------------------------------------------------------------


def test_deposit_groups_payments_once_and_void_releases_them(w):
    p1, p2 = _owner_payment(w, 1000), _owner_payment(w, 2500)
    batch = acc.create_deposit(entry_ids=[p1.pk, p2.pk], deposited_on=TODAY, actor=w["manager"])
    assert batch.total_cents == 3500 and acc.undeposited_payments() == []
    with pytest.raises(acc.AccountingError):
        acc.create_deposit(entry_ids=[p1.pk], deposited_on=TODAY, actor=w["manager"])
    acc.void_deposit(batch, actor=w["manager"])
    assert {e.pk for e in acc.undeposited_payments()} == {p1.pk, p2.pk}
    with pytest.raises(AppendOnlyError):
        batch.delete()
    with pytest.raises(acc.AccountingError):
        acc.void_deposit(batch, actor=w["manager"])


def test_owner_cannot_record_deposits(w):
    p = _owner_payment(w, 1000)
    with pytest.raises(PermissionDenied):
        acc.create_deposit(entry_ids=[p.pk], deposited_on=TODAY, actor=w["owner"])


# --- Reconciliation ------------------------------------------------------------------------


def test_reconciliation_with_outstanding_items_balances(w):
    acc.save_setup(
        gl_accounts="",
        opening_cash_cents=1_000_000,
        opening_cash_on=TODAY - datetime.timedelta(days=30),
        actor=w["board"],
    )
    batch = acc.create_deposit(
        entry_ids=[_owner_payment(w, 5000).pk], deposited_on=TODAY, actor=w["manager"]
    )
    cleared = _pay_vendor(w, 20000)
    _pay_vendor(w, 30000)  # check not yet cashed
    book = acc.book_balance(TODAY)
    assert book == 1_000_000 + 5000 - 50000
    # Bank shows the opening balance less the cashed check; the deposit is in transit.
    rec = acc.reconcile(
        statement_date=TODAY,
        statement_balance_cents=1_000_000 - 20000,
        cleared_deposit_ids=[],
        cleared_payment_ids=[cleared.pk],
        actor=w["manager"],
    )
    assert rec.difference_cents == 0
    assert rec.outstanding_deposits_cents == batch.total_cents
    left = acc.uncleared(TODAY)
    assert [p.pk for p in left["payments"]] != [cleared.pk] and len(left["deposits"]) == 1
    with pytest.raises(acc.AccountingError):  # already cleared
        acc.reconcile(
            statement_date=TODAY,
            statement_balance_cents=0,
            cleared_deposit_ids=[],
            cleared_payment_ids=[cleared.pk],
            actor=w["manager"],
        )
    with pytest.raises(acc.AccountingError):  # a reconciled deposit cannot be voided
        acc.reconcile(
            statement_date=TODAY,
            statement_balance_cents=1_000_000 - 20000 + 5000 - 30000,
            cleared_deposit_ids=[batch.pk],
            cleared_payment_ids=[],
            actor=w["manager"],
        ) and acc.void_deposit(batch, actor=w["manager"])


def test_reconcile_page(client, w):
    client.force_login(w["manager"])
    assert client.get(reverse("reconcile") + f"?date={TODAY}").status_code == 200
    client.force_login(w["owner"])
    assert client.get(reverse("reconcile")).status_code == 403


# --- 1099 ------------------------------------------------------------------------------------


def test_1099_worksheet_flags_vendors_over_the_threshold(w):
    _pay_vendor(w, 150000)
    rows = acc.vendor_1099_totals(TODAY.year)
    assert rows[0]["total_cents"] == 150000 and not rows[0]["over"]
    _pay_vendor(w, 60000)
    row = acc.vendor_1099_totals(TODAY.year)[0]
    assert row["over"] and not row["w9"]
    data = acc.vendor_1099_csv(TODAY.year, actor=w["board"]).decode()
    assert "'=Mow Co" in data and "2100.00" in data


# --- Courtesy reminders -------------------------------------------------------------------


def test_courtesy_reminders_only_when_set_and_once_a_month(w):
    services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=4500,
        description="Dues",
        posted_on=TODAY - datetime.timedelta(days=20),
        actor=w["board"],
    )
    day20 = TODAY.replace(day=20)
    assert acc.send_courtesy_reminders(today=day20) == 0  # off by default
    community.set_rule(
        key="collections.courtesy_reminder_day", value="15", citation="Board", actor=w["board"]
    )
    assert acc.send_courtesy_reminders(today=TODAY.replace(day=10)) == 0  # too early
    from django.test import TestCase

    with TestCase.captureOnCommitCallbacks(execute=True):
        assert acc.send_courtesy_reminders(today=day20) == 1
    assert acc.send_courtesy_reminders(today=day20) == 0
    assert [m.to for m in mail.outbox] == [[w["owner"].email]]
    assert "4500" not in mail.outbox[0].body and "$45.00" in mail.outbox[0].body


# --- Collections letter sequence ------------------------------------------------------------


def test_letter_sequence_first_second_final_then_board_decides(w):
    services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=9000,
        description="Dues",
        posted_on=TODAY - datetime.timedelta(days=20),
        actor=w["board"],
    )
    case = services.open_case(lot=w["lot"], actor=w["manager"])
    kinds = []
    clock = [TODAY]
    for _ in range(3):
        with mock.patch("ledger.accounting.timezone.localdate", lambda *a: clock[0]):
            data, kind = acc.generate_case_letter(case, actor=w["manager"])
        assert data.startswith(b"%PDF")
        kinds.append(kind)
        case.refresh_from_db()
        with (
            mock.patch("ledger.accounting.timezone.localdate", lambda *a: clock[0]),
            pytest.raises(acc.AccountingError),  # the notice period runs first
        ):
            acc.generate_case_letter(case, actor=w["manager"])
        clock[0] += datetime.timedelta(days=11)
    assert kinds == ["first", "second", "final"] and case.state == "final_notice"
    with pytest.raises(acc.AccountingError):
        acc.generate_case_letter(case, actor=w["manager"])
    with system_access("t"):
        assert CollectionLetter.objects.filter(case=case).count() == 3
    with pytest.raises(PermissionDenied):  # referral stays with the board
        services.COLLECTION.apply(case, "refer", actor=w["manager"], note="x")
    services.COLLECTION.apply(case, "refer", actor=w["board"], note="To counsel")


def test_letters_due_waits_the_notice_period(w):
    services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=9000,
        description="Dues",
        posted_on=TODAY - datetime.timedelta(days=20),
        actor=w["board"],
    )
    case = services.open_case(lot=w["lot"], actor=w["manager"])
    assert [d["kind"] for d in acc.letters_due()] == ["first"]
    acc.generate_case_letter(case, actor=w["manager"])
    assert acc.letters_due() == []
    later = TODAY + datetime.timedelta(days=11)
    assert [d["kind"] for d in acc.letters_due(today=later)] == ["second"]


def test_case_letter_view_downloads_and_logs(client, w):
    services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=9000,
        description="Dues",
        posted_on=TODAY - datetime.timedelta(days=20),
        actor=w["board"],
    )
    case = services.open_case(lot=w["lot"], actor=w["manager"])
    client.force_login(w["manager"])
    page = client.get(reverse("collection_detail", args=[case.pk])).content
    assert b"First delinquency notice" in page
    response = client.post(reverse("collection_letter", args=[case.pk]))
    assert response["Content-Type"] == "application/pdf"
    client.force_login(w["owner"])
    assert client.post(reverse("collection_letter", args=[case.pk])).status_code in (403, 404)


def test_accounting_page(client, w):
    _owner_payment(w, 1000)
    client.force_login(w["manager"])
    page = client.get(reverse("accounting")).content
    assert b"Payments to deposit" in page and b"1099 worksheet" in page
    client.force_login(w["owner"])
    assert client.get(reverse("accounting")).status_code == 403
    assert DepositBatch.objects.count() == 0


# --- Regression review fixes (BUG-006, BUG-016, SEC-007, BUG-014) --------------------------


def test_charges_dated_later_this_month_are_not_past_due(w):
    services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=4500,
        description="Due on the 15th",
        posted_on=TODAY + datetime.timedelta(days=5),
        actor=w["board"],
    )
    assert services.past_due(w["lot"].pk) == 0
    assert services.past_due(w["lot"].pk, TODAY + datetime.timedelta(days=6)) == 4500


def test_a_bounced_deposited_payment_leaves_book_cash(w):
    payment = _owner_payment(w, 5000)
    acc.create_deposit(entry_ids=[payment.pk], deposited_on=TODAY, actor=w["manager"])
    before = acc.book_balance(TODAY)
    services.reverse_entry(payment, reason="Returned check", actor=w["board"])
    assert acc.book_balance(TODAY) == before - 5000
    assert len(acc.uncleared(TODAY)["returned"]) == 1


def test_export_uses_the_shared_formula_guard(w):
    from core.lists import safe_cell

    w["vendor"].name = "\t=cmd"
    w["vendor"].save()
    _pay_vendor(w, 100)
    text = acc.vendor_1099_csv(TODAY.year, actor=w["board"]).decode()
    assert safe_cell("\t=cmd") in text


def test_long_letter_settings_are_trimmed_not_a_500(w):
    from community.models import CommunityProfile

    community.update_letter_settings(
        letter_contact="x" * 5000, resale_insurance_company="y" * 5000, actor=w["board"]
    )
    profile = CommunityProfile.get()
    assert (
        len(profile.letter_contact) <= CommunityProfile._meta.get_field("letter_contact").max_length
    )

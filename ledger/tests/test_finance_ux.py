"""UX review: owner account page, Record payments batch page, one aging
bucket set, one Finance tab bar, month picker for dues, and the other
finance page fixes."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import CommunityProfile, Role
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import services
from ledger.models import DepositBatch, EntryKind, LedgerEntry

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = timezone.localdate()


@pytest.fixture
def w():
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="7500", citation="Declaration §7.2 (demo)", actor=board
    )
    lot = LotFactory(street_address="10 Oak Lane")
    lot_b = LotFactory(street_address="12 Oak Lane")
    return {
        "board": board,
        "lot": lot,
        "lot_b": lot_b,
        "owner": make_member(lots=[lot]),
        "owner_b": make_member(lots=[lot_b]),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
    }


def _entries(lot):
    with system_access("test"):
        return list(LedgerEntry.objects.filter(lot=lot))


# --- Aging buckets ---------------------------------------------------------------------------


def test_aging_uses_one_set_of_buckets():
    assert services.BUCKETS == ("current", "1_30", "31_60", "61_90", "over_90")
    labels = list(services.BUCKET_LABELS.values())
    assert labels[1] == "1\u201330 days" and labels[-1] == "Over 90 days"
    assert labels[0] == "Current" and labels[2:4] == ["31\u201360 days", "61\u201390 days"]


@pytest.mark.parametrize(
    ("age", "bucket"),
    [(0, "current"), (1, "1_30"), (30, "1_30"), (31, "31_60"), (61, "61_90"), (91, "over_90")],
)
def test_aging_bucket_boundaries(w, age, bucket):
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=1000,
        description="x",
        posted_on=TODAY - datetime.timedelta(days=age),
        actor=w["board"],
    )
    a = services.aging(w["lot"].pk)
    assert a[bucket] == 1000 and sum(a[b] for b in services.BUCKETS) == 1000


def test_lot_page_and_collections_show_the_same_buckets(client, w):
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=1000,
        description="x",
        posted_on=TODAY - datetime.timedelta(days=45),
        actor=w["board"],
    )
    client.force_login(w["board"])
    for url in (reverse("lot_ledger", args=[w["lot"].pk]), reverse("collections")):
        page = client.get(url).content.decode()
        for label in services.BUCKET_LABELS.values():
            assert label in page, (url, label)
    report = client.get(reverse("report_run", args=["ar_aging"])).content.decode()
    assert "1\u201330 days" in report and "Over 90 days" in report


def _owing(w):
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=9000,
        description="Gate share",
        posted_on=TODAY - datetime.timedelta(days=45),
        actor=w["board"],
    )


def _cases(lot):
    from ledger.models import CollectionCase

    with system_access("test"):
        return list(CollectionCase.objects.filter(lot=lot))


def test_lot_page_has_no_letter_buttons_only_open_collection_case(client, w):
    """Delinquency letters come only from the collection case (streamlining)."""
    _owing(w)
    client.force_login(w["board"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert "menu-details" not in page and "notice-buttons" not in page
    for kind in ("first", "second", "final"):
        assert reverse("lot_notice_pdf", args=[w["lot"].pk, kind]) not in page
    assert page.count("Open collection case") == 1
    assert reverse("lot_ledger_action", args=[w["lot"].pk, "collection"]) in page


def test_open_collection_case_creates_it_once_then_goes_to_it(client, w):
    _owing(w)
    client.force_login(w["manager"])
    url = reverse("lot_ledger_action", args=[w["lot"].pk, "collection"])
    first = client.post(url)
    [case] = _cases(w["lot"])
    assert first.status_code == 302 and first.url == reverse("collection_detail", args=[case.pk])
    second = client.post(url)
    assert second.url == reverse("collection_detail", args=[case.pk])
    assert len(_cases(w["lot"])) == 1
    assert AuditEvent.objects.filter(action="collection.opened").count() == 1
    # With a case open, the lot page links straight to it.
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert reverse("collection_detail", args=[case.pk]) in page
    assert url not in page


def test_only_collections_staff_open_a_case(client, w):
    _owing(w)
    url = reverse("lot_ledger_action", args=[w["lot"].pk, "collection"])
    client.force_login(w["auditor"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert "Open collection case" not in page
    assert client.post(url).status_code == 403
    client.force_login(w["owner"])
    assert client.post(url).status_code in (403, 404)
    assert _cases(w["lot"]) == []


def test_old_lot_notice_address_redirects_to_the_case(client, w):
    _owing(w)
    client.force_login(w["board"])
    old = reverse("lot_notice_pdf", args=[w["lot"].pk, "first"])
    # No case yet: back to the lot's account with a pointer; nothing generated.
    response = client.get(old)
    assert response.status_code == 302
    assert response.url == reverse("lot_ledger", args=[w["lot"].pk])
    case = services.open_case(lot=w["lot"], actor=w["board"])
    response = client.get(old)
    assert response.status_code == 302
    assert response.url == reverse("collection_detail", args=[case.pk])
    assert not AuditEvent.objects.filter(action="ledger.notice_generated").exists()


def test_case_page_reprints_from_the_case(client, w):
    _owing(w)
    case = services.open_case(lot=w["lot"], actor=w["board"])
    client.force_login(w["board"])
    page = client.get(reverse("collection_detail", args=[case.pk])).content.decode()
    for kind in ("statement", "first", "second", "final"):
        assert reverse("collection_notice_pdf", args=[case.pk, kind]) in page
    assert "/notice-" not in page
    response = client.get(reverse("collection_notice_pdf", args=[case.pk, "final"]))
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    event = AuditEvent.objects.get(action="ledger.notice_generated")
    assert event.metadata == {"kind": "final", "lot_id": w["lot"].pk}


# --- My account --------------------------------------------------------------------------------


def test_my_account_statement_has_charges_payments_and_running_balance(client, w):
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=30000,
        description="Pool key",
        posted_on=TODAY - datetime.timedelta(days=3),
        actor=w["board"],
    )
    services.record_payment(lot=w["lot"], amount_cents=10000, received_on=TODAY, actor=w["manager"])
    client.force_login(w["owner"])
    page = client.get(reverse("my_account")).content.decode()
    assert '<th class="num">Charges</th>' in page and '<th class="num">Payments</th>' in page
    assert '<th class="num">Balance</th>' in page
    assert "-$100.00" not in page  # a payment is not shown as a negative amount
    assert "$300.00" in page and "$100.00" in page and "$200.00" in page


def test_statement_rows_keep_a_running_balance(w):
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=5000,
        description="a",
        posted_on=D(2026, 1, 5),
        actor=w["board"],
    )
    services.record_payment(
        lot=w["lot"], amount_cents=8000, received_on=D(2026, 1, 9), actor=w["manager"]
    )
    rows = services.statement_rows(services.owner_statement(w["lot"].pk, D(2026, 1, 1)))
    assert [(r.charge_cents, r.payment_cents, r.balance_cents) for r in rows] == [
        (5000, 0, 5000),
        (0, 8000, -3000),
    ]


def test_my_account_hides_pay_online_and_fees_when_payments_are_off(client, w):
    community.set_rule(
        key="payments.credit_card_surcharge_bp",
        value="300",
        citation="Board resolution",
        actor=SYSTEM,
    )
    services.post_charge(
        lot=w["lot"], kind="other_charge", amount_cents=10000, description="x", actor=w["board"]
    )
    client.force_login(w["owner"])
    page = client.get(reverse("my_account")).content.decode()
    assert "Pay online" not in page
    assert "surcharge" not in page


def test_my_account_shows_pay_online_and_fees_when_payments_work(client, w):
    community.set_rule(
        key="payments.credit_card_surcharge_bp",
        value="300",
        citation="Board resolution",
        actor=SYSTEM,
    )
    services.post_charge(
        lot=w["lot"], kind="other_charge", amount_cents=10000, description="x", actor=w["board"]
    )
    client.force_login(w["owner"])
    with (
        mock.patch("ledger.views.online_payments_enabled", return_value=True),
        mock.patch("ledger.views.payable_lot_ids", return_value=frozenset({w["lot"].pk})),
    ):
        page = client.get(reverse("my_account")).content.decode()
    assert "Pay online" in page and reverse("pay_online", args=[w["lot"].pk]) in page
    assert "surcharge" in page and "$103.00" in page
    assert "Bank account (no fee)" in page and "Debit card (no fee)" in page


def test_my_account_says_who_to_pay_and_where_to_mail(client, w):
    profile = CommunityProfile.get()
    profile.remit_to = "Example HOA\nPO Box 12\nExampletown, PA 00000"
    profile.save()
    client.force_login(w["owner"])
    page = client.get(reverse("my_account")).content.decode()
    assert "Make checks payable to <strong>" + profile.legal_name in page
    assert "PO Box 12" in page and "Exampletown, PA 00000" in page


def test_my_account_without_remit_address_says_to_ask_the_board(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("my_account")).content.decode()
    assert "Ask the board where to send checks." in page


def test_amount_due_names_the_due_date(client, w):
    due = TODAY + datetime.timedelta(days=5)
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=7500,
        description="Dues",
        posted_on=due,
        actor=w["board"],
    )
    info = services.amount_due(w["lot"].pk)
    assert info.amount_cents == 7500 and info.due_on == due and not info.past_due
    client.force_login(w["owner"])
    with mock.patch("ledger.services.timezone.localdate", return_value=TODAY):
        page = client.get(reverse("my_account")).content.decode()
    assert f"Due by {due:%b} {due.day}, {due.year}" in page


def test_amount_due_reports_past_due_and_paid_up(w):
    old = TODAY - datetime.timedelta(days=40)
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=7500,
        description="Dues",
        posted_on=old,
        actor=w["board"],
    )
    info = services.amount_due(w["lot"].pk)
    assert info.past_due and info.due_on == old
    services.record_payment(lot=w["lot"], amount_cents=7500, received_on=TODAY, actor=w["manager"])
    assert services.amount_due(w["lot"].pk).amount_cents == 0


# --- Record payments (batch) -------------------------------------------------------------------


def _row(i, lot, amount, check="", date=""):
    # The home is typed by street address (D96).
    return {f"lot_{i}": lot.label, f"amount_{i}": amount, f"check_{i}": check, f"date_{i}": date}


@pytest.mark.parametrize("who", ["board", "manager"])
def test_board_and_manager_can_open_record_payments(client, w, who):
    client.force_login(w[who])
    page = client.get(reverse("record_payments"))
    assert page.status_code == 200
    # The shared home picker (D96): no long list of every home on the page.
    assert b'name="lot_0"' in page.content and b"data-home-picker" in page.content
    assert b"(Lot" not in page.content


@pytest.mark.parametrize("who", ["owner", "auditor"])
def test_owner_and_auditor_cannot_record_payments(client, w, who):
    client.force_login(w[who])
    assert client.get(reverse("record_payments")).status_code == 403
    data = {"rows": "1", **_row(0, w["lot_b"], "75.00", "101")}
    assert client.post(reverse("record_payments"), data).status_code == 403
    assert _entries(w["lot_b"]) == []


def test_record_payments_posts_every_row_and_audits(client, w):
    client.force_login(w["manager"])
    data = {
        "rows": "3",
        **_row(0, w["lot"], "75.00", "101", str(TODAY)),
        **_row(1, w["lot_b"], "$1,150.50", "202"),
    }
    response = client.post(reverse("record_payments"), data)
    assert response.status_code == 302 and "recorded=" in response["Location"]
    a, b = _entries(w["lot"]), _entries(w["lot_b"])
    assert [(e.amount_cents, e.reference, e.kind) for e in a] == [(-7500, "101", EntryKind.PAYMENT)]
    assert [(e.amount_cents, e.reference) for e in b] == [(-115050, "202")]
    assert AuditEvent.objects.filter(action="ledger.payment_recorded").count() == 2
    batch = AuditEvent.objects.get(action="ledger.payments_batch_recorded")
    assert batch.metadata == {"count": 2, "total_cents": 122550}
    page = client.get(response["Location"]).content.decode()
    assert "Add these to a deposit" in page and "$1,225.50" in page


def test_record_payments_with_a_bad_row_records_nothing(client, w):
    client.force_login(w["board"])
    tomorrow = TODAY + datetime.timedelta(days=1)
    data = {
        "rows": "4",
        **_row(0, w["lot"], "75.00", "101"),
        **_row(1, w["lot_b"], "seventy"),
        **_row(2, w["lot_b"], "10.00", "", str(tomorrow)),
        **_row(3, w["lot"], "10.00", "123456789012"),
    }
    response = client.post(reverse("record_payments"), data)
    page = response.content.decode()
    assert response.status_code == 200
    assert "Row 2: Enter the amount in dollars" in page
    assert "Row 3: The date received cannot be in the future." in page
    assert "Row 4: Enter a check number, never an account number." in page
    assert 'value="75.00"' in page  # what was typed is kept
    assert _entries(w["lot"]) == [] and _entries(w["lot_b"]) == []


def test_record_payments_refuses_a_check_already_recorded(client, w):
    services.record_payment(
        lot=w["lot"], amount_cents=7500, received_on=TODAY, reference="101", actor=w["board"]
    )
    client.force_login(w["manager"])
    page = client.post(
        reverse("record_payments"), {"rows": "1", **_row(0, w["lot"], "75.00", "101")}
    ).content.decode()
    assert "already recorded" in page
    assert len(_entries(w["lot"])) == 1


# --- BUG-006: a bounced (reversed) check may be deposited again ------------------


def test_record_payments_accepts_a_bounced_check_presented_again(client, w):
    bounced = services.record_payment(
        lot=w["lot"], amount_cents=7500, received_on=TODAY, reference="1042", actor=w["board"]
    )
    services.reverse_entry(bounced, reason="Check returned", actor=w["board"])
    client.force_login(w["manager"])
    response = client.post(
        reverse("record_payments"), {"rows": "1", **_row(0, w["lot"], "75.00", "1042")}
    )
    assert response.status_code == 302
    assert "already recorded" not in response.content.decode()
    assert len(_entries(w["lot"])) == 3  # payment, reversal, payment again


def test_single_payment_form_follows_the_same_duplicate_rule(client, w):
    first = services.record_payment(
        lot=w["lot"], amount_cents=7500, received_on=TODAY, reference="1042", actor=w["board"]
    )
    with pytest.raises(services.LedgerError, match="already recorded"):
        services.record_payment(
            lot=w["lot"], amount_cents=7500, received_on=TODAY, reference="1042", actor=w["board"]
        )
    # The same check number on another home is a different check.
    services.record_payment(
        lot=w["lot_b"], amount_cents=7500, received_on=TODAY, reference="1042", actor=w["board"]
    )
    services.reverse_entry(first, reason="Check returned", actor=w["board"])
    again = services.record_payment(
        lot=w["lot"], amount_cents=7500, received_on=TODAY, reference="1042", actor=w["board"]
    )
    assert again.amount_cents == -7500
    client.force_login(w["board"])
    client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "payment"]),
        {
            "amount_cents": "75.00",
            "received_on": TODAY.isoformat(),
            "method": "check",
            "reference": "1042",
        },
    )
    assert len(_entries(w["lot"])) == 3  # the duplicate from the form was refused


def test_record_payments_needs_at_least_one_row(client, w):
    client.force_login(w["manager"])
    response = client.post(reverse("record_payments"), {"rows": "2"})
    assert b"Enter at least one payment." in response.content


def test_record_payments_then_add_them_to_a_deposit(client, w):
    client.force_login(w["manager"])
    response = client.post(
        reverse("record_payments"),
        {"rows": "2", **_row(0, w["lot"], "75.00", "101"), **_row(1, w["lot_b"], "50", "7")},
    )
    ids = response["Location"].split("recorded=")[1].split(",")
    response = client.post(
        reverse("record_payments"),
        {"section": "deposit", "entries": ids, "deposited_on": str(TODAY)},
    )
    assert response.status_code == 302
    batch = DepositBatch.objects.get()
    assert batch.total_cents == 12500
    page = client.get(reverse("record_payments") + "?recorded=" + ",".join(ids)).content
    assert b"Add these to a deposit" not in page  # already deposited


def test_recorded_list_is_scoped_to_payments(client, w):
    charge = services.post_charge(
        lot=w["lot"], kind="other_charge", amount_cents=999, description="x", actor=w["board"]
    )
    client.force_login(w["manager"])
    page = client.get(reverse("record_payments") + f"?recorded={charge.pk}").content
    assert b"$9.99" not in page


# --- Monthly dues with a month picker ---------------------------------------------------------


def test_dues_form_uses_a_month_picker_and_previews_the_charge(client, w):
    client.force_login(w["manager"])
    page = client.get(reverse("ledger_home")).content.decode()
    # Months in words, not the browser's month grid (D48).
    assert 'type="month"' not in page and '<select id="dperiod" name="period"' in page
    nxt = (TODAY.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    assert f'value="{nxt:%Y-%m}" selected>{nxt:%B %Y}</option>' in page
    assert "Will charge <strong>2 homes $75.00 each</strong>" in page


def test_posting_dues_from_the_month_picker(client, w):
    client.force_login(w["manager"])
    client.post(reverse("ledger_home"), {"period": "2026-03"})
    entries = _entries(w["lot"])
    assert [(e.period, e.amount_cents) for e in entries] == [("2026-03", 7500)]
    client.post(reverse("ledger_home"), {"period": "garbage"})
    assert len(_entries(w["lot"])) == 1


# --- One Finance tab bar ----------------------------------------------------------------------


def test_every_finance_page_shows_the_same_tabs(client, w):
    """Streamlining 2026-11-26: tabs only across Owner accounts, Collections,
    Accounting and Reports; the rest of Finance is in the sidebar."""
    client.force_login(w["board"])
    tabs = ["Owner accounts", "Collections", "Accounting", "Reports"]
    for name in ("ledger_home", "accounting", "collections", "reports", "reconcile"):
        page = client.get(reverse(name)).content.decode()
        nav = page.split('aria-label="Finance"')[1].split("</nav>")[0]
        assert [t for t in tabs if f">{t}</a>" in nav] == tabs, name
        assert ">Invoices</a>" not in nav and ">Letters</a>" not in nav


def test_finance_tabs_leave_out_switched_off_features(client, w):
    """Pages outside the four (other Finance pages, Maintenance) have no tab
    bar at all: the sidebar is the one menu."""
    client.force_login(w["board"])
    for name in ("invoices", "contracts", "vendors", "work_orders", "budgets", "letters_settings"):
        page = client.get(reverse(name)).content.decode()
        assert 'aria-label="Finance"' not in page, name


def test_manager_does_not_see_the_letters_tab(client, w):
    client.force_login(w["manager"])
    page = client.get(reverse("ledger_home")).content.decode()
    nav = page.split('aria-label="Finance"')[1].split("</nav>")[0]
    assert ">Letters</a>" not in nav and ">Owner accounts</a>" in nav


# --- Accounting setup and 1099 table ----------------------------------------------------------


def test_accounting_setup_has_one_labeled_input_per_account(client, w):
    from ledger import accounting as acc

    client.force_login(w["board"])
    # The setup is its own tab of Accounting (owner request 2026-11-26).
    page = client.get(reverse("accounting"), {"tab": "setup"}).content.decode()
    assert 'name="gl_accounts"' not in page
    for key in acc.GL_LABELS:
        assert f'<label for="id_gl_{key}">' in page and f'name="gl_{key}"' in page
    assert 'placeholder="1000 Operating cash"' in page


def test_accounting_setup_saves_each_account_into_the_stored_text(client, w):
    from ledger.models import AccountingSetup

    client.force_login(w["manager"])
    client.post(
        reverse("accounting"),
        {
            "section": "setup",
            "gl_cash": "1010 Main checking",
            "gl_assessment": "",
            "opening_cash": "0",
            "opening_cash_on": "2026-01-01",
        },
    )
    accounts = AccountingSetup.get().accounts()
    assert accounts["cash"] == "1010 Main checking"
    assert accounts["assessment"] == "4000 Assessment income"  # blank keeps the default


def test_1099_table_scrolls_on_phones(client, w):
    from operations.models import ExpenseCategory, Invoice, InvoicePayment, Vendor

    vendor = Vendor.objects.create(name="Mow Co", w9_on_file=False)
    invoice = Invoice.objects.create(
        vendor=vendor,
        number="A1",
        invoice_date=TODAY,
        amount_cents=250000,
        category=ExpenseCategory.objects.create(code="5100", name="Landscaping"),
        entered_by=w["manager"],
        state="paid",
    )
    InvoicePayment.objects.create(
        invoice=invoice, paid_on=TODAY, method="check", amount_cents=250000, recorded_by=w["board"]
    )
    client.force_login(w["board"])
    page = client.get(reverse("accounting") + f"?year={TODAY.year}").content.decode()
    section = page.split("<h2>1099 worksheet</h2>")[1]
    assert '<div class="table-wrap"><table class="stack-sm">' in section and "Mow Co" in section
    assert 'class="table small"' not in page


# --- Letters: one Save per section, unsaved-changes warning -------------------------------------


def test_letters_sections_warn_about_unsaved_changes(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("letters_settings")).content.decode()
    forms = page.count('name="section"')
    assert forms >= 2 and page.count("data-warn-unsaved=") == forms
    assert page.count("data-unsaved-note hidden") == forms


def test_app_js_has_the_unsaved_changes_warning():
    from pathlib import Path

    from django.conf import settings

    js = (Path(settings.BASE_DIR) / "static" / "js" / "app.js").read_text()
    assert "function portalWarnUnsaved()" in js and "beforeunload" in js


# --- Collection case: one shared note ----------------------------------------------------------


def test_collection_case_has_one_shared_note_for_every_step(client, w):
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=5000,
        description="x",
        posted_on=TODAY - datetime.timedelta(days=60),
        actor=w["board"],
    )
    case = services.open_case(lot=w["lot"], actor=w["board"])
    client.force_login(w["board"])
    page = client.get(reverse("collection_detail", args=[case.pk])).content.decode()
    assert page.count('name="note"') == 1
    # D76: a payment plan is set up on its own page; its approval moves the case.
    assert reverse("payment_plan_new", args=[case.pk]) in page
    assert reverse("collection_action", args=[case.pk, "close"]) in page
    assert "required for" in page
    response = client.post(
        reverse("collection_action", args=[case.pk, "close"]),
        {"note": "Paid in full"},
    )
    assert response.status_code == 302
    case.refresh_from_db()
    assert case.state == "closed"


# --- Invoice: the file comes before the approval buttons --------------------------------------


def test_invoice_file_is_shown_above_the_approval_buttons(client, w):
    from operations.models import ExpenseCategory, Invoice, Vendor

    invoice = Invoice.objects.create(
        vendor=Vendor.objects.create(name="Pond Co"),
        number="P-9",
        invoice_date=TODAY,
        amount_cents=12000,
        category=ExpenseCategory.objects.create(code="5200", name="Pond"),
        entered_by=w["manager"],
        state="pending_approval",
    )
    client.force_login(w["board"])
    page = client.get(reverse("invoice_detail", args=[invoice.pk])).content.decode()
    assert "Your approval" in page
    assert page.index("Invoice file") < page.index("Your approval")
    assert "Ask for the invoice before approving." in page


# --- Record payments received quick action (WT-010) ----------------------------------------


@pytest.mark.parametrize(
    ("who", "shown"), [("board", True), ("manager", True), ("auditor", False), ("owner", False)]
)
def test_record_checks_quick_action_is_for_board_and_manager(w, who, shown):
    from django.template import Context, Template

    from core import my_items

    actions = my_items.quick_actions(w[who])
    html = Template("{% load portal %}{% quick_actions %}").render(Context({"user": w[who]}))
    if shown:
        assert actions == [
            {
                "label": "Record payments received",
                "url": reverse("record_payments"),
                "icon": "receipt",
            }
        ]
        assert f'href="{reverse("record_payments")}"' in html and "Record payments received" in html
    else:
        assert actions == [] and html.strip() == ""

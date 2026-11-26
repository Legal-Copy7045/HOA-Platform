import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Ownership, Person, Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.scoping import UnscopedQueryError, system_access
from ledger import services
from ledger.models import LedgerEntry
from operations.models import ExpenseCategory, Invoice, Vendor

pytestmark = pytest.mark.django_db
D = datetime.date


@pytest.fixture
def w():
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2 (demo)", actor=board
    )
    lot = LotFactory()
    return {
        "board": board,
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "manager": make_member(roles=[Role.MANAGER]),
    }


def test_monthly_dues_post_once_per_lot_per_month(w):
    assert services.post_monthly_assessments(year=2026, month=1, actor=w["manager"]) == 1
    assert services.post_monthly_assessments(year=2026, month=1, actor=w["manager"]) == 0
    assert services.balance(w["lot"].pk) == 4500


def test_dues_need_the_rule():
    board = make_member(board=True)
    with pytest.raises(services.LedgerError, match="monthly dues"):
        services.post_monthly_assessments(year=2026, month=1, actor=board)


def test_prepayment_is_drawn_down_month_by_month(w):
    services.record_payment(
        lot=w["lot"],
        amount_cents=4500 * 12,
        received_on=D(2025, 12, 20),
        reference="1001",
        actor=w["manager"],
    )
    for month in (1, 2, 3):
        services.post_monthly_assessments(year=2026, month=month, actor=w["manager"])
    assert services.balance(w["lot"].pk) == -4500 * 9  # nine months of credit left
    assert services.aging(w["lot"].pk, as_of=D(2026, 3, 15))["credit"] == 4500 * 9


def test_aging_applies_credits_to_the_oldest_charges_first(w):
    for month in (1, 2, 3, 4):
        services.post_monthly_assessments(year=2026, month=month, actor=w["manager"])
    services.record_payment(
        lot=w["lot"], amount_cents=4500, received_on=D(2026, 4, 2), actor=w["manager"]
    )
    a = services.aging(w["lot"].pk, as_of=D(2026, 4, 15))
    # January paid; Feb (73 days) 61-90, Mar (45 days) 31-60, Apr (14 days) 1-30.
    assert a == {
        "current": 0,
        "1_30": 4500,
        "31_60": 4500,
        "61_90": 4500,
        "over_90": 0,
        "credit": 0,
    }


def test_late_fees_stop_when_the_emergency_switch_is_off(w, settings):
    """Decision D21: late fees are allowed; LEDGER_LATE_FEES_ENABLED=false stops them."""
    settings.LEDGER_LATE_FEES_ENABLED = False
    with pytest.raises(services.LedgerError):
        services.post_charge(
            lot=w["lot"],
            kind="late_fee",
            amount_cents=600,
            description="Late fee",
            actor=w["board"],
        )
    settings.LEDGER_LATE_FEES_ENABLED = True
    services.post_charge(
        lot=w["lot"], kind="late_fee", amount_cents=600, description="Late fee", actor=w["board"]
    )


def test_corrections_are_reversals_never_edits(w):
    entry = services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=2500,
        description="Key fob",
        actor=w["manager"],
    )
    with pytest.raises(AppendOnlyError):
        entry.delete()
    reversal = services.reverse_entry(entry, reason="Entered on wrong lot", actor=w["manager"])
    assert services.balance(w["lot"].pk) == 0
    with pytest.raises(services.LedgerError, match="already been reversed"):
        services.reverse_entry(entry, reason="again", actor=w["manager"])
    with pytest.raises(services.LedgerError, match="cannot itself"):
        services.reverse_entry(reversal, reason="undo", actor=w["manager"])


def test_account_numbers_refused_on_payments(w):
    with pytest.raises(services.LedgerError, match="account number"):
        services.record_payment(
            lot=w["lot"],
            amount_cents=100,
            received_on=D(2026, 1, 1),
            reference="123456789012",
            actor=w["manager"],
        )


def test_owners_cannot_post_and_ledger_is_scoped(w):
    with pytest.raises(PermissionDenied):
        services.post_credit(lot=w["lot"], amount_cents=100, description="x", actor=w["owner"])
    with pytest.raises(UnscopedQueryError):
        LedgerEntry.objects.count()
    other = make_member(lots=[LotFactory()])
    services.post_monthly_assessments(year=2026, month=1, actor=w["manager"])
    assert not LedgerEntry.objects.for_user(other).filter(lot=w["lot"]).exists()


def test_new_owner_starts_clean_and_the_sellers_arrears_stay_on_the_old_account(client, w):
    """D33-acctfix-1 (replaces the earlier "brought forward" behaviour): the
    seller's unpaid dues stay on the seller's, now dormant, account."""
    services.post_monthly_assessments(year=2026, month=1, actor=w["manager"])
    buyer = make_member()
    with system_access("t"):
        buyer_person = Person.objects.get(user=buyer)
    community.transfer_lot(
        w["lot"], new_owners=[buyer_person], transfer_date=D(2026, 2, 15), actor=SYSTEM
    )
    st = services.owner_statement(w["lot"].pk, D(2026, 2, 15))
    assert st.brought_forward == 0 and st.entries == [] and st.balance == 0
    assert services.balance(w["lot"].pk) == 4500  # still on the lot, on the seller's account
    client.force_login(buyer)
    page = client.get(reverse("my_account")).content.decode()
    assert "Balance brought forward" not in page and "Assessment January 2026" not in page
    assert "Paid up" in page
    client.force_login(w["board"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert "Still owed by former owner: <strong>$45.00</strong>" in page
    with system_access("t"):
        assert not Ownership.objects.filter(person__user=w["owner"]).active().exists()


def test_budget_lock_and_actuals(w):
    cat = ExpenseCategory.objects.create(code="5100", name="Pond")
    budget = services.create_budget(fiscal_year=2026, actor=w["board"])
    services.add_line(
        budget,
        kind="expense",
        name="Pond care",
        annual_cents=120000,
        category=cat,
        actor=w["board"],
    )
    services.add_line(
        budget,
        kind="income",
        name="Dues",
        annual_cents=4500 * 12,
        income_source="assessments",
        actor=w["board"],
    )
    services.adopt_budget(budget, actor=w["board"])
    with pytest.raises(services.LedgerError, match="adopted"):
        services.add_line(
            budget, kind="expense", name="x", annual_cents=1, category=cat, actor=w["board"]
        )
    Invoice.objects.create(
        vendor=Vendor.objects.create(name="V"),
        number="1",
        invoice_date=D(2026, 3, 3),
        amount_cents=15000,
        category=cat,
        state="paid",
        entered_by=w["manager"],
    )
    services.post_monthly_assessments(year=2026, month=3, actor=w["manager"])
    report = services.budget_vs_actual(budget)
    expense = next(r for r in report["rows"] if r["line"].kind == "expense")
    income = next(r for r in report["rows"] if r["line"].kind == "income")
    assert expense["actual"][2] == 15000 and expense["planned"][2] == 10000
    assert income["actual_total"] == 4500 and income["planned_total"] == 54000


def test_resale_certificate_fee_posted_on_issue(w):
    community.set_rule(key="resale.fee_cents", value="15000", citation="Demo", actor=w["board"])
    community.set_rule(key="resale.deadline_days", value="10", citation="Demo", actor=w["board"])
    req = services.request_resale(lot=w["lot"], requester_name="Seller", actor=w["owner"])
    assert req.fee_cents == 15000 and req.due_on
    services.RESALE.apply(req, "issue", actor=w["manager"])
    assert services.balance(w["lot"].pk) == 15000
    assert services.resale_certificate(req).startswith(b"%PDF")


def test_owner_requests_resale_only_for_own_lot(w):
    with pytest.raises(services.LedgerError):
        services.request_resale(lot=LotFactory(), requester_name="x", actor=w["owner"])


# --- Security review 2026-11 ------------------------------------------------------


def test_bounced_payment_restores_the_age_of_the_old_charge(w):
    """D2: the reversal was aged as a new charge dated today."""
    old = timezone.localdate() - datetime.timedelta(days=120)
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=10000,
        description="Old charge",
        posted_on=old,
        actor=w["board"],
    )
    payment = services.record_payment(
        lot=w["lot"], amount_cents=10000, received_on=old, actor=w["board"]
    )
    services.reverse_entry(payment, reason="Check returned", actor=w["board"])
    aging = services.aging(w["lot"].pk)
    assert aging["over_90"] == 10000
    assert aging["current"] == 0


def test_reversed_mistaken_charge_does_not_pay_down_old_debt(w):
    old = timezone.localdate() - datetime.timedelta(days=120)
    services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=10000,
        description="Old",
        posted_on=old,
        actor=w["board"],
    )
    mistake = services.post_charge(
        lot=w["lot"], kind="other_charge", amount_cents=5000, description="Oops", actor=w["board"]
    )
    services.reverse_entry(mistake, reason="Entered on wrong lot", actor=w["board"])
    aging = services.aging(w["lot"].pk)
    assert aging["over_90"] == 10000 and aging["current"] == 0


@pytest.mark.parametrize("value", ["²", "1" * 20, "-5", "4.5"])
def test_rule_values_must_be_plain_whole_numbers(w, value):
    """D9: "²" passed isdigit() and then broke every read of the rule."""
    with pytest.raises((ValueError, community.CommunityError)):
        community.set_rule(key="dues.monthly_cents", value=value, citation="x", actor=w["board"])


def test_only_one_open_collection_case_per_lot(w):
    """D10: enforced by a database constraint, not only a check-then-insert."""
    from django.db import IntegrityError, transaction

    from ledger.models import CollectionCase

    services.open_case(lot=w["lot"], actor=w["board"])
    with pytest.raises(IntegrityError), transaction.atomic(), system_access("t"):
        CollectionCase.objects.create(lot=w["lot"], opened_by=w["board"])


def test_issued_resale_certificate_is_stored_and_served_unchanged(w, client):
    """D5: every download used to rebuild the certificate from today's data."""
    community.set_rule(key="resale.fee_cents", value="15000", citation="Demo", actor=w["board"])
    req = services.request_resale(lot=w["lot"], requester_name="Seller", actor=w["owner"])
    services.save_disclosures(req, "Pending roof claim (demo)", actor=w["manager"])
    req = services.RESALE.apply(req, "issue", actor=w["manager"])
    stored = services.issued_certificate(req)
    assert stored is not None
    with pytest.raises(services.LedgerError):
        services.save_disclosures(req, "changed later", actor=w["manager"])
    # Something changes after issue: the certificate given to the buyer does not.
    services.post_charge(
        lot=w["lot"], kind="other_charge", amount_cents=999, description="Later", actor=w["board"]
    )
    client.force_login(w["owner"])
    response = client.get(reverse("resale_pdf", args=[req.pk]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == stored.file.open("rb").read()


# --- BUG-005: dues posted ahead of time are not listed again as upcoming ---------


def test_dues_posted_early_are_not_listed_again_as_upcoming(w):
    on = D(2026, 11, 26)
    before = services.upcoming_charges(0, lot_id=w["lot"].pk, on=on)
    assert [c.due_on for c in before] == [D(2026, 12, 1), D(2027, 1, 1), D(2027, 2, 1)]
    services.post_monthly_assessments(year=2026, month=12, actor=w["manager"])
    balance = services.balance(w["lot"].pk)
    assert balance == 4500  # December is in the balance ...
    after = services.upcoming_charges(balance, lot_id=w["lot"].pk, on=on)
    # ... and not listed a second time.
    assert [c.due_on for c in after] == [D(2027, 1, 1), D(2027, 2, 1), D(2027, 3, 1)]


def test_reversed_early_assessment_is_listed_as_upcoming_again(w):
    on = D(2026, 11, 26)
    services.post_monthly_assessments(year=2026, month=12, actor=w["manager"])
    with system_access("test"):
        entry = LedgerEntry.objects.get(lot=w["lot"], period="2026-12")
    services.reverse_entry(entry, reason="Posted by mistake", actor=w["board"])
    upcoming = services.upcoming_charges(0, lot_id=w["lot"].pk, on=on)
    assert upcoming[0].due_on == D(2026, 12, 1)


def test_owner_ruling_prepaid_months_listed_unpaid_and_drawn_down_as_posted(client, w):
    """Owner ruling 2026-11-26: a prepaid owner's future months are listed as
    not yet paid; the prepayment is drawn down only as each month posts."""
    services.record_payment(
        lot=w["lot"], amount_cents=4500 * 3, received_on=D(2026, 11, 2), actor=w["manager"]
    )
    on = D(2026, 11, 26)
    balance = services.balance(w["lot"].pk)
    assert balance == -4500 * 3
    upcoming = services.upcoming_charges(balance, lot_id=w["lot"].pk, on=on)
    # Every future month is still listed as a charge; nothing is marked paid.
    assert [(c.due_on, c.amount_cents) for c in upcoming] == [
        (D(2026, 12, 1), 4500),
        (D(2027, 1, 1), 4500),
        (D(2027, 2, 1), 4500),
    ]
    services.post_monthly_assessments(year=2026, month=12, actor=w["manager"])
    balance = services.balance(w["lot"].pk)
    assert balance == -4500 * 2  # the credit drops by one month's dues
    upcoming = services.upcoming_charges(balance, lot_id=w["lot"].pk, on=on)
    assert [c.due_on for c in upcoming] == [D(2027, 1, 1), D(2027, 2, 1), D(2027, 3, 1)]
    assert [c.covered_by_credit for c in upcoming] == [True, True, False]


def test_my_items_says_due_by_for_dues_posted_ahead(w):
    from core.my_items import items_for

    services.post_monthly_assessments(year=2099, month=1, actor=w["manager"])
    items = [i for i in items_for(w["owner"]) if i.nav == "my_account"]
    assert len(items) == 1
    assert "due by" in items[0].detail and "due since" not in items[0].detail
    assert items[0].tone == "info"

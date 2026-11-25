import datetime

import pytest
from django.core.exceptions import PermissionDenied

from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.workflow import WorkflowError
from operations import services
from operations.models import ExpenseCategory, Vendor

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 11, 20)


@pytest.fixture
def w():
    return {
        "manager": make_member(roles=[Role.MANAGER]),
        "b1": make_member(board=True),
        "b2": make_member(board=True),
        "b3": make_member(board=True),
        "vendor": Vendor.objects.create(name="Pond Pros (demo)"),
        "cat": ExpenseCategory.objects.create(code="5100", name="Pond maintenance"),
    }


def invoice(w, cents, by="manager", number="1001"):
    inv = services.create_invoice(
        vendor=w["vendor"],
        number=number,
        invoice_date=TODAY,
        amount_cents=cents,
        category=w["cat"],
        actor=w[by],
    )
    return services.INVOICE.apply(inv, "submit", actor=w[by])


def test_under_500_needs_one_approval(w):
    inv = services.approve(invoice(w, 49999), actor=w["b1"])
    assert inv.state == "approved"


def test_500_and_over_needs_two_different_board_members(w):
    inv = invoice(w, 50000)
    inv = services.approve(inv, actor=w["b1"])
    assert inv.state == "pending_approval"
    with pytest.raises(services.OperationsError, match="different board member"):
        services.approve(inv, actor=w["b1"])
    inv = services.approve(inv, actor=w["b2"])
    assert inv.state == "approved"


def test_threshold_is_a_rule(w):
    community.set_rule(
        key="invoice.dual_approval_cents",
        value="100000",
        citation="Board resolution (demo)",
        actor=w["b1"],
    )
    assert services.approve(invoice(w, 50000), actor=w["b1"]).state == "approved"


def test_entrant_never_approves(w):
    inv = invoice(w, 10000, by="b1")
    with pytest.raises(services.OperationsError, match="entered this invoice"):
        services.approve(inv, actor=w["b1"])
    with pytest.raises(WorkflowError):
        services.reject(inv, note="no", actor=w["b1"])


def test_manager_and_auditor_cannot_approve(w):
    inv = invoice(w, 10000, by="b1")
    for roles in ([Role.MANAGER], [Role.AUDITOR]):
        with pytest.raises(PermissionDenied):
            services.approve(inv, actor=make_member(roles=roles))


def test_payments_append_only_and_mark_paid(w):
    inv = services.approve(invoice(w, 30000), actor=w["b1"])
    with pytest.raises(services.OperationsError, match="remaining"):
        services.record_payment(
            inv,
            paid_on=TODAY,
            method="check",
            amount_cents=30001,
            reference="1042",
            actor=w["manager"],
        )
    p1 = services.record_payment(
        inv, paid_on=TODAY, method="check", amount_cents=10000, reference="1042", actor=w["manager"]
    )
    inv.refresh_from_db()
    assert inv.state == "approved"
    services.record_payment(
        inv, paid_on=TODAY, method="check", amount_cents=20000, reference="1043", actor=w["manager"]
    )
    inv.refresh_from_db()
    assert inv.state == "paid"
    with pytest.raises(AppendOnlyError):
        p1.delete()


@pytest.mark.parametrize("ref", ["021000021 123456789", "Acct 123456789012"])
def test_account_numbers_are_refused(w, ref):
    inv = services.approve(invoice(w, 1000), actor=w["b1"])
    with pytest.raises(services.OperationsError, match="account number"):
        services.record_payment(
            inv, paid_on=TODAY, method="ach", amount_cents=1000, reference=ref, actor=w["manager"]
        )


def test_unpaid_invoices_cannot_be_paid_before_approval(w):
    inv = invoice(w, 1000)
    with pytest.raises(services.OperationsError, match="approved"):
        services.record_payment(
            inv, paid_on=TODAY, method="check", amount_cents=1000, actor=w["manager"]
        )


def test_duplicate_invoice_numbers_per_vendor_refused(w):
    invoice(w, 1000, number="A-1")
    with pytest.raises(services.OperationsError, match="already has"):
        invoice(w, 2000, number="A-1")


def test_owners_cannot_touch_finance():
    with pytest.raises(PermissionDenied):
        services.create_work_order(title="x", actor=make_member(lots=[LotFactory()]))


def test_work_order_flow(w):
    wo = services.create_work_order(title="Clear pond outlet", actor=w["manager"])
    with pytest.raises(WorkflowError, match="vendor"):
        services.WORK_ORDER.apply(wo, "assign", actor=w["manager"])
    services.set_work_order_vendor(wo, w["vendor"], actor=w["manager"])
    services.WORK_ORDER.apply(wo, "assign", actor=w["manager"])
    services.WORK_ORDER.apply(wo, "complete", actor=w["manager"], note="Done Tuesday")
    wo.refresh_from_db()
    assert wo.state == "completed"


# --- Security review 2026-11 ------------------------------------------------------


def test_approvals_needed_are_fixed_when_submitted(w):
    """A6: one board member could clear the threshold, then approve alone."""
    community.set_rule(
        key="invoice.dual_approval_cents",
        value="50000",
        citation="Board res. (demo)",
        actor=w["b1"],
    )
    inv = invoice(w, 90000)
    assert services.approvals_required(inv) == 2
    community.set_rule(key="invoice.dual_approval_cents", value="", citation="", actor=w["b1"])
    inv = services.approve(inv, actor=w["b1"])
    assert inv.state == "pending_approval"  # still needs a second, different approver
    assert inv.approvals.get().amount_cents == 90000  # D6: the amount approved is recorded


def test_submitted_invoice_amount_cannot_change(w):
    """D6: enforced by a database trigger, not just the forms."""
    from django.db import IntegrityError, transaction

    from operations.models import Invoice

    inv = invoice(w, 10000)
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=inv.pk).update(amount_cents=1)


@pytest.mark.parametrize("amount", [-5000, 0])
def test_invoice_payment_must_be_positive(w, amount):
    """D8."""
    inv = services.approve(invoice(w, 10000), actor=w["b1"])
    with pytest.raises(services.OperationsError):
        services.record_payment(
            inv, paid_on=TODAY, method="check", amount_cents=amount, actor=w["manager"]
        )

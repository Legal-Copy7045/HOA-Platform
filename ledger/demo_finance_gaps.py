"""Demo data for special assessments, payment plans, reimbursements, vendor
checks and next year's budget (D75-D78). Fictional only; called once from
the oak_creek dataset after seed_demo_operations. Every step goes through
the services, as the right person (never one's own matter)."""

from __future__ import annotations

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.policy import SYSTEM
from core.scoping import system_access

DEMO = "DEMO - replace"
_PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _receipt(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _PDF, content_type="application/pdf")


def seed() -> dict[str, int]:
    """Create the demo records; returns counts. Safe to skip if already run."""
    from community.models import BoardSeat, Lot
    from core import conflicts
    from ledger.models import SpecialAssessment

    with system_access("finance gaps demo"):
        if SpecialAssessment.objects.exists():
            return {}
        board = [
            s.person.user
            for s in BoardSeat.objects.active().select_related("person__user")
            if s.person.user_id
        ]
        if len(board) < 2:
            return {}
        lots = {lot.number: lot for lot in Lot.objects.all()}

        def staff_for(matter, *, not_user=None):
            return next(
                (u for u in board if u != not_user and not conflicts.own_matter(u, matter)),
                SYSTEM,
            )

        out = {}
        out["special"] = _special_assessment(lots, staff_for)
        out["plans"] = _payment_plans(lots, staff_for)
        out["claims"], out["checks"] = _reimbursements_and_checks(board, staff_for)
        out["budget"] = _next_budget(board)
    return out


def _special_assessment(lots, staff_for) -> int:
    from ledger import services as ledger
    from ledger import special_assessments as sa

    t = timezone.localdate()
    levy = sa.levy(
        title="Pond dredging (demo)",
        purpose="Dredge the north detention pond and rebuild its outlet (fictional demo).",
        total_cents=600 * 100 * len(lots),
        due_dates=[
            t - datetime.timedelta(days=60),
            t - datetime.timedelta(days=30),
            t + datetime.timedelta(days=30),
            t + datetime.timedelta(days=60),
        ],
        fund="reserve",
        resolution_ref="Resolution 2026-20 (demo)",
        actor=SYSTEM,
    )
    # Some homes paid both installments posted so far, some one, the rest none.
    for number in (5, 6, 8, 9, 10, 11, 14, 15):
        lot = lots.get(number)
        if lot is None:
            continue
        amount = 30000 if number % 2 else 15000
        ledger.record_payment(
            lot=lot,
            amount_cents=amount,
            received_on=t - datetime.timedelta(days=5),
            reference=f"SA-{number}",
            actor=staff_for(lot),
        )
    return levy.pk


def _payment_plans(lots, staff_for) -> int:
    from ledger import payment_plans
    from ledger import services as ledger
    from ledger.models import CollectionCase

    t = timezone.localdate()
    made = 0
    for number, first_due, start in (
        (19, t + datetime.timedelta(days=10), t),  # current
        (33, t - datetime.timedelta(days=75), t - datetime.timedelta(days=80)),  # defaults
    ):
        lot = lots.get(number)
        if lot is None:
            continue
        existing = CollectionCase.objects.filter(lot=lot).exclude(state="closed").first()
        if existing is not None and payment_plans.plan_for_case(existing) is not None:
            made += 1  # the demo states already agreed this home's plan
            continue
        ledger.post_charge(
            lot=lot,
            kind="other_charge",
            amount_cents=36000,
            description="Unpaid landscaping charge (demo)",
            posted_on=t - datetime.timedelta(days=120),
            actor=staff_for(lot),
        )
        case = CollectionCase.objects.filter(lot=lot).exclude(state="closed").first()
        if case is None:
            case = ledger.open_case(lot=lot, actor=staff_for(lot))
        _charges, total, _account = payment_plans.covered_charges(case)
        preparer = staff_for(lot)
        plan = payment_plans.prepare(
            case,
            installments=payment_plans.even_installments(total, 3, first_due),
            start_on=start,
            note="Agreed by phone (demo).",
            actor=preparer,
        )
        payment_plans.approve(plan, actor=staff_for(lot, not_user=preparer))
        made += 1
    payment_plans.check_plans()  # the second one defaults back to its earlier state
    return made


def _reimbursements_and_checks(board, staff_for) -> tuple[int, int]:
    from operations import reimbursements as rb
    from operations import services as ops
    from operations import vendor_checks as vc
    from operations.models import ExpenseCategory, Vendor

    t = timezone.localdate()
    cat = ExpenseCategory.objects.filter(active=True).order_by("code").first()
    if cat is None:
        return 0, 0
    b1, b2 = board[0], board[1]
    b3 = board[2] if len(board) > 2 else board[1]
    claims = []
    for who, amount, payee, what in (
        (b1, 4825, "Hardware store (demo)", "Paint and brushes for the pavilion"),
        (b2, 12900, "Garden centre (demo)", "Mulch for the entrance beds"),
        (b1, 2350, "Print shop (demo)", "Copies of the annual meeting notice"),
        (b2, 61000, "Rental yard (demo)", "Pressure washer rental"),
    ):
        claims.append(
            rb.submit(
                amount_cents=amount,
                spent_on=t - datetime.timedelta(days=12),
                payee=payee,
                category=cat,
                description=f"{what} (demo)",
                files=[_receipt("receipt.pdf")],
                actor=who,
            )
        )
    pending, approved, paid, declined = claims
    rb.approve(approved, actor=b2 if approved.submitted_by_id != b2.pk else b1)
    rb.approve(paid, actor=b2)
    rb.pay_by_transfer(
        paid,
        paid_on=t - datetime.timedelta(days=2),
        reference="TRF-DEMO-1",
        actor=b3 if b3 != b1 else b2,
    )
    rb.decline(declined, note="Not an association expense (demo).", actor=b1)
    del pending  # stays waiting for approval

    vendor = Vendor.objects.create(
        name="Clearwater Pond Services (demo)",
        address="12 Fictional Road\nAnytown, PA 00000",
        services="Pond maintenance",
    )
    invoices = []
    for number, amount in (("CPS-101", 42000), ("CPS-102", 18500), ("CPS-103", 9900)):
        inv = ops.create_invoice(
            vendor=vendor,
            number=number,
            invoice_date=t - datetime.timedelta(days=20),
            amount_cents=amount,
            category=cat,
            description="Monthly pond care (demo)",
            actor=b1,
        )
        ops.INVOICE.apply(inv, "submit", actor=b1)
        ops.approve(inv, actor=b2)
        if ops.approvals_required(inv) > 1:
            ops.approve(inv, actor=b3)
        invoices.append(inv)
    issued = vc.issue(
        [i.pk for i in invoices],
        first_number="5101",
        issued_on=t - datetime.timedelta(days=10),
        actor=b2,
    )
    vc.mark_cleared(issued[0], on=t - datetime.timedelta(days=3), actor=b2)
    vc.void_check(issued[2], reason="Printed with the wrong date (demo)", actor=b3)
    return len(claims), len(issued)


def _next_budget(board) -> int:
    from ledger import budget_prep
    from ledger.models import Budget

    latest = Budget.objects.order_by("-fiscal_year").first()
    if latest is None or Budget.objects.filter(fiscal_year=latest.fiscal_year + 1).exists():
        return 0
    draft = budget_prep.prepare_next(latest, basis="higher", increase_bp=300, actor=board[0])
    return draft.pk

"""Fictional demo data for the finance options (D80-D85), added to the
oak_creek dataset after seed_demo_operations (one call in community/demo).

Switches the six features on and records: interest posted for two months on
a delinquent account, a 12-month forecast with exactly one warning month, a
collection case referred to an attorney with a lien filed (and a cost
charged to the owner) and another whose lien was released after payment, a
capital project in progress with a change order, and two insurance claims
(one under review, one paid). All names and figures are made up.
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from core.policy import SYSTEM
from core.scoping import system_access

DEMO = "DEMO - replace"


def _board_logins() -> list:
    from community.models import BoardSeat

    with system_access("demo finance options"):
        seats = BoardSeat.objects.active().select_related("person__user")
        return [s.person.user for s in seats if s.person.user_id]


def _staff_for(board, matter):
    from core import conflicts

    return next((u for u in board if not conflicts.own_matter(u, matter)), board[0])


def seed() -> str:
    from community import services as community
    from core import features

    board = _board_logins()
    if len(board) < 2:
        return "Finance options demo skipped (needs two board logins)."
    for key in (
        "overdue_interest",
        "cashflow_forecast",
        "collections_legal",
        "capital_projects",
        "owner_finance_dashboard",
        "insurance_claims",
    ):
        features.set_feature(key, True, actor=SYSTEM)
    for key, value, citation in (
        ("interest.annual_rate_bp", "1000", "Declaration §6.6 (demo)"),
        ("interest.grace_days", "30", DEMO),
        ("interest.min_balance_cents", "1000", DEMO),
        ("legal.costs_chargeable", "true", "Declaration §6.7 (demo)"),
        ("capital.second_approval_cents", "500000", DEMO),
        ("insurance.second_approval_cents", "500000", DEMO),
    ):
        community.set_rule(key=key, value=value, citation=citation, actor=SYSTEM)
    _interest()
    _legal(board)
    _project(board)
    _claims(board)
    _forecast()
    return "Finance options demo data created (interest, forecast, liens, a project, two claims)."


def _interest() -> None:
    from ledger import interest
    from ledger.cashflow import add_months

    today = timezone.localdate()
    for back in (2, 1):
        month = add_months(today.replace(day=1), -back)
        interest.post_interest(year=month.year, month=month.month, actor=SYSTEM)


def _lot(number):
    from community.models import Lot

    with system_access("demo finance options"):
        return Lot.objects.get(number=number)


def _legal(board) -> None:
    from ledger import legal, services
    from ledger.models import CollectionCase

    today = timezone.localdate()
    # Lot 27: referred, lien filed, a chargeable cost charged to the account.
    lot = _lot(27)
    staff = _staff_for(board, lot)
    with system_access("demo finance options"):
        case = CollectionCase.objects.filter(lot=lot).exclude(state="closed").first()
    if case is None:
        case = services.open_case(lot=lot, actor=staff)
    for step in ("send_reminder", "send_second_reminder"):
        services.COLLECTION.apply(case, step, actor=staff)
        case.refresh_from_db()
    services.COLLECTION.apply(case, "refer", actor=staff, note="Referred to counsel (demo)")
    case.refresh_from_db()
    legal.record(
        case,
        kind="referral",
        occurred_on=today - datetime.timedelta(days=40),
        firm="Example & Partners LLP (demo)",
        attorney="A. Example (demo)",
        reference="EX-2026-0142",
        fee_arrangement="Flat fee per stage (demo)",
        actor=staff,
    )
    legal.record(case, kind="demand", occurred_on=today - datetime.timedelta(days=35), actor=staff)
    legal.record(
        case,
        kind="lien",
        occurred_on=today - datetime.timedelta(days=20),
        lien_filing="statement",
        court="common_pleas",
        reference="2026-LN-00077 (demo)",
        amount_cents=services.balance(lot.pk),
        actor=staff,
    )
    legal.record(
        case,
        kind="hearing",
        occurred_on=today,
        due_on=today + datetime.timedelta(days=12),
        detail="Status hearing (demo)",
        actor=staff,
    )
    cost = legal.record(
        case,
        kind="cost",
        occurred_on=today - datetime.timedelta(days=20),
        amount_cents=35000,
        chargeable=True,
        detail="Lien filing and counsel's demand letter (demo)",
        actor=staff,
    )
    legal.post_cost(cost, actor=staff)

    # Lot 15: referred, lien filed, paid in full, lien released, case closed.
    lot = _lot(15)
    staff = _staff_for(board, lot)
    case = services.open_case(lot=lot, actor=staff)
    for step in ("send_reminder", "send_second_reminder"):
        services.COLLECTION.apply(case, step, actor=staff)
        case.refresh_from_db()
    services.COLLECTION.apply(case, "refer", actor=staff, note="Referred to counsel (demo)")
    case.refresh_from_db()
    legal.record(
        case,
        kind="referral",
        occurred_on=today - datetime.timedelta(days=60),
        firm="Example & Partners LLP (demo)",
        reference="EX-2026-0101",
        actor=staff,
    )
    legal.record(
        case,
        kind="lien",
        occurred_on=today - datetime.timedelta(days=50),
        lien_filing="statement",
        court="common_pleas",
        reference="2026-LN-00031 (demo)",
        amount_cents=services.balance(lot.pk),
        actor=staff,
    )
    owed = services.balance(lot.pk)
    if owed > 0:
        services.record_payment(
            lot=lot, amount_cents=owed, received_on=today, reference="7001", actor=staff
        )
        legal.record(case, kind="recovery", amount_cents=owed, occurred_on=today, actor=staff)
    legal.record(
        case,
        kind="release",
        occurred_on=today,
        reference="2026-LN-00031 satisfied (demo)",
        actor=staff,
    )
    services.COLLECTION.apply(case, "close", actor=staff, note="Paid in full; lien released (demo)")


def _project(board) -> None:
    from operations import capital_projects as cp
    from operations.models import Invoice, Vendor

    b1, b2 = board[0], board[1]
    project = cp.create(
        title="North pond dredging (demo)",
        description="Remove silt from the north detention pond (demo).",
        budget_cents=4800000,
        funding="reserve",
        actor=b1,
    )
    cp.ask_approval(project, resolution_ref="Resolution 2026-21 (demo)", actor=b1)
    cp.approve(project, actor=b1)
    cp.approve(project, actor=b2)
    cp.change_state(project, "start", actor=b1)
    vendor = Vendor.objects.filter(name="Pond Pros (demo)").first()
    if vendor is not None:
        cp.link(project, kind="vendor", object_id=vendor.pk, committed_cents=4650000, actor=b1)
    paid = Invoice.objects.filter(number="PP-101", state="paid").first()
    if paid is not None:
        cp.link(project, kind="invoice", object_id=paid.pk, actor=b1)
    co = cp.propose_change(
        project, amount_cents=240000, reason="Extra silt found at the outlet (demo)", actor=b1
    )
    cp.decide_change(co, decision="approve", actor=b2)
    cp.update(project, percent_complete=40, owner_visible=True, actor=b1)


def _claims(board) -> None:
    from community.models import CommonArea
    from operations import insurance_claims as ic

    b1, b2 = board[0], board[1]
    today = timezone.localdate()
    pond = CommonArea.objects.filter(name="North detention pond").first()
    ic.create(
        carrier="Fictional Insurance Co (demo)",
        policy_number="GL-000123 (demo)",
        incident_on=today - datetime.timedelta(days=9),
        discovered_on=today - datetime.timedelta(days=8),
        description="Storm toppled the entrance sign (demo).",
        location="Main entrance",
        claim_number="CLM-2026-0456 (demo)",
        adjuster_name="J. Adjuster (demo)",
        adjuster_contact="adjuster@example.com",
        estimate_cents=980000,
        deductible_cents=100000,
        proof_of_loss_due=today + datetime.timedelta(days=10),
        actor=b1,
    )
    claim = ic.create(
        carrier="Fictional Insurance Co (demo)",
        policy_number="PR-000789 (demo)",
        incident_on=today - datetime.timedelta(days=120),
        description="Fallen tree damaged the pond fence (demo).",
        common_area=pond,
        claim_number="CLM-2026-0311 (demo)",
        estimate_cents=1400000,
        deductible_cents=100000,
        actor=b1,
    )
    ic.change_state(claim, "review", actor=b1)
    offer = ic.record_offer(claim, amount_cents=1300000, note="Less deductible (demo)", actor=b1)
    ic.approve_offer(offer, actor=b1)
    ic.approve_offer(offer, actor=b2)
    ic.record_money(
        claim,
        kind="received",
        amount_cents=1300000,
        on=today - datetime.timedelta(days=20),
        reference="Insurer check 55012 (demo)",
        actor=b1,
    )


def _forecast() -> None:
    """Set the operating cash floor so exactly one forecast month is flagged."""
    from community import services as community
    from ledger import cashflow

    closes = sorted({r.operating_close for r in cashflow.build().rows})
    if len(closes) < 2:
        return
    floor = (closes[0] + closes[1]) // 2 + 1
    community.set_rule(
        key="cashflow.min_operating_cents", value=str(max(floor, 0)), citation=DEMO, actor=SYSTEM
    )

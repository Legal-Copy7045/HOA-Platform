"""Money in every state: late fees, credits, reversals,
collection cases at each step, resale certificates,
deposits and a reconciliation, refunds and a surplus distribution at each
step, next year's draft budget and a confirmed bank-feed match."""

from __future__ import annotations

import datetime

from django.conf import settings

from community.demo import DemoError
from community.demo.states import current_account

# lot -> collection case state after seeding (lot 27's case, open, is the
# first part's: its first letter is ready to generate).
CASES = {
    15: "reminder_sent",
    16: "second_reminder",
    17: "final_notice",
    19: "payment_plan",
    21: "referred",
    22: "closed",
}
# lot -> what the refund for that home is waiting for.
REFUNDS = {
    5: "approval (the vice president's own home: another board member decides)",
    9: "its check (approved)",
    20: "clearing (check issued)",
    24: "nothing (cleared)",
    25: "a new check (the first was voided)",
    28: "nothing (declined)",
    32: "approval (fresh)",
}
FRESH_CREDIT_LOT = 30  # a credit nobody has proposed to refund yet


def seed(c) -> None:
    from ledger import accounting, disbursements
    from ledger import services as ledger
    from ledger.models import CollectionCase

    manager, pres, treas = (c.u(k) for k in ("manager", "president", "treasurer"))
    lots = c.lots

    # Arrears for the collection cases: a repair charge from 75 days ago.
    for n in CASES:
        if n == 15:
            continue  # already behind on assessments
        ledger.post_charge(
            lot=lots[n],
            kind="other_charge",
            amount_cents=32500,
            description="Curb and driveway apron repair (demo)",
            posted_on=c.days(-75),
            actor=manager,
        )
    # Late fees for last month's unpaid assessments (lots 15 and 27).
    last = c.t.replace(day=1) - datetime.timedelta(days=1)
    if settings.LEDGER_LATE_FEES_ENABLED:
        ledger.post_late_fees(year=last.year, month=last.month, actor=treas)

    # A goodwill credit, and a charge posted by mistake then reversed.
    ledger.post_credit(
        lot=lots[33],
        amount_cents=1500,
        description="Missed trash pickup credit (demo)",
        actor=manager,
    )
    wrong = ledger.post_charge(
        lot=lots[34],
        kind="other_charge",
        amount_cents=4500,
        description="Gate remote (posted to the wrong home) (demo)",
        actor=manager,
    )
    ledger.reverse_entry(wrong, reason="Wrong home (demo).", actor=manager)

    # A payment moved to another account of the home (reassign_entry): the
    # seller of lot 31 paid their last bill after the sale, so the board
    # moves it from the buyer's account back to the seller's (D87: the
    # rebuild of history replays moves too).
    from community.demo.oak_creek import TRANSFERRED_LOT
    from community.models import Account

    sold = lots[TRANSFERRED_LOT]
    seller_account = Account.objects.filter(lot=sold, closed_on__isnull=False).first()
    if seller_account is not None:
        late = ledger.record_payment(
            lot=sold,
            amount_cents=4500,
            received_on=c.days(-30),
            reference="SETTLE-31",
            actor=manager,
        )
        ledger.reassign_entry(
            late,
            account_id=seller_account.pk,
            reason="The seller's last payment, sent after the sale (demo).",
            actor=treas,
        )

    # --- Collection cases at every step ------------------------------------------------
    C = ledger.COLLECTION
    for n, state in CASES.items():
        case = ledger.open_case(lot=lots[n], notes="Demo case.", actor=manager)
        if state == "closed":
            C.apply(case, "close", actor=manager, note="Paid in full (demo).")
            continue
        accounting.generate_case_letter(case, actor=manager)  # first reminder
        case.refresh_from_db()
        if state in ("second_reminder", "final_notice", "referred"):
            C.apply(case, "send_second_reminder", actor=manager)
        if state == "final_notice":
            C.apply(case, "send_final_notice", actor=manager)
        if state == "payment_plan":
            C.apply(
                case, "payment_plan", actor=manager, note="$100 a month from next month (demo)."
            )
        if state == "referred":
            C.apply(case, "refer", actor=treas, note="Sent to the association's attorney (demo).")
        case.refresh_from_db()
        if case.state != state:
            raise DemoError(f"collection case {n}: {case.state}, not {state}")
    ledger.set_case_follow_up(CollectionCase.objects.get(lot=lots[19]), c.days(25), actor=manager)

    # --- Resale certificates -----------------------------------------------------------
    R = ledger.RESALE
    resale = ledger.request_resale(
        lot=lots[7], requester_name="Joint Seller (demo)", actor=c.u("joint_a")
    )
    R.apply(resale, "start", actor=manager)
    resale = ledger.request_resale(lot=lots[3], requester_name="Casey (demo)", actor=c.u("owner"))
    ledger.save_disclosures(resale, "No pending litigation (demo).", actor=manager)
    R.apply(resale, "issue", actor=manager)
    resale = ledger.request_resale(
        lot=lots[15], requester_name="Seller (demo)", actor=c.u("owner_arc")
    )
    R.apply(resale, "cancel", actor=manager, note="Sale fell through (demo).")

    # --- Deposits and a bank reconciliation -------------------------------------------
    waiting = [p.pk for p in accounting.undeposited_payments()]
    if waiting:
        deposit = accounting.create_deposit(
            entry_ids=waiting[:20], deposited_on=c.t, bank_reference="DEP-1 (demo)", actor=manager
        )
        accounting.reconcile(
            statement_date=c.t,
            statement_balance_cents=accounting.book_balance(c.t),
            cleared_deposit_ids=[deposit.pk],
            cleared_payment_ids=[],
            note="Demo reconciliation.",
            actor=treas,
        )

    # --- Refunds and a surplus distribution --------------------------------------------
    def overpay(n, cents):
        ledger.record_payment(
            lot=lots[n],
            amount_cents=cents,
            received_on=c.t,
            reference=f"OVER-{n}",
            actor=manager,
        )

    def refund(n, cents):
        account = current_account(lots[n])
        available = disbursements.account_credit(account) - disbursements.reserved_cents(account.pk)
        return disbursements.propose_refund(
            account=account,
            amount_cents=min(cents, available),
            note="Overpayment refund (demo).",
            actor=manager,
        )

    def checks(batch):
        from ledger.models import DisbursementCheck

        return list(DisbursementCheck.objects.filter(line__batch=batch).order_by("pk"))

    own = c.home("vice_president")  # 5, unless bootstrap_first_officer ran first
    for n in (own, 20, 24, 25, 28, 32, FRESH_CREDIT_LOT):
        overpay(n, 9000)
    refund(own, 9000)  # waiting; own matter for the vice president
    refund(32, 9000)  # waiting, fresh: any board member may approve
    batch = refund(9, 4500)
    disbursements.approve(batch, actor=pres)
    numbers = iter(range(5001, 5100))
    for n in (20, 24, 25):
        batch = refund(n, 9000)
        disbursements.approve(batch, actor=pres)
        disbursements.issue_checks(
            batch, first_number=str(next(numbers)), issued_on=c.t, actor=manager
        )
        check = checks(batch)[0]
        if n == 24:
            disbursements.mark_cleared(check, on=c.t, actor=manager)
        if n == 25:
            disbursements.void_check(check, reason="Lost in the mail (demo).", actor=treas)
    batch = refund(28, 9000)
    disbursements.decline(batch, note="The owner asked us to keep it as a credit.", actor=treas)
    # A surplus distribution as statement credits, waiting for approval.
    from governance.models import Resolution

    resolution = Resolution.objects.filter(kind="resolution").order_by("pk").first()
    # D67: split by what was paid in the fiscal year; the demo's assessments
    # may only cover this year, so use the latest year with payments. With
    # allocation suspended (a rebuild test) nothing counts as paid yet, and
    # the proposal is left out.
    year = c.t.year - 1 if disbursements.assessments_paid(c.t.year - 1) else c.t.year
    if disbursements.assessments_paid(year):
        disbursements.propose_surplus(
            total_cents=44 * 2500,
            year=year,
            resolution=resolution,
            note="Last year's operating surplus (demo).",
            actor=manager,
        )

    # --- Next year's budget, still a draft --------------------------------------------
    from operations.models import ExpenseCategory

    budget = ledger.create_budget(fiscal_year=c.t.year + 1, actor=treas)
    cats = {cat.code: cat for cat in ExpenseCategory.objects.all()}
    for kind, name, cents, extra in (
        ("income", "Assessments", 4700 * 12 * len(lots), {"income_source": "assessments"}),
        ("expense", "Pond and stormwater", 620000, {"category": cats["5100"]}),
        ("expense", "Landscaping", 950000, {"category": cats["5200"]}),
        ("expense", "Insurance", 380000, {"category": cats["5300"]}),
    ):
        ledger.add_line(budget, kind=kind, name=name, annual_cents=cents, actor=treas, **extra)

    # --- Bank feed: confirm one suggested match, leave the rest to match --------------
    from ledger.bankfeeds import matching

    for row in matching.suggestions():
        if row["candidates"]:
            cand = row["candidates"][0]
            matching.confirm(row["line"], kind=cand.kind, object_id=cand.object_id, actor=manager)
            break

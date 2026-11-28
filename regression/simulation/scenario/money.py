"""Money: payments by profile, deposits, Stripe, the bank, invoices, checks,
reimbursements, collections and legal, payment plans, special assessment,
budget preparation, reserve spending, the injected close problem, year end.

Assessments, late fees, interest, special-assessment installments, payment
plan checks, autopay charges, Stripe and bank syncs and the month and year
close are the portal's own scheduled tasks (engine.py fires them); this
module is what people and the outside world do around them.
"""

from __future__ import annotations

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.scoping import system_access
from regression.simulation.scenario import setup

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
SKIP = "skip"


def _receipt() -> SimpleUploadedFile:
    return SimpleUploadedFile("receipt.pdf", PDF, content_type="application/pdf")


def _lot(home):
    from community.models import Lot

    with system_access("simulation lookup"):
        return Lot.objects.get(pk=home.lot_id)


def due_now(home, today) -> int:
    """What the current owners' account owes today (letters and pages use CURRENT)."""
    from ledger import accounts
    from ledger import services as ledger

    with system_access("simulation lookup"):
        return max(0, ledger.balance(home.lot_id, account=accounts.CURRENT))


def plan(sc, day, rng):
    w = sc.world
    m = sc.month_index(day)
    out = []
    ev = sc.ev
    if day == sc.o.start:
        return out  # setup day
    # --- owners pay ---------------------------------------------------------------------
    for home in w.owner_homes():
        p = home.profile
        r = rng.random()
        if p in ("on_time", "paid_off") and day.day == 1 + (home.number % 9):
            out.append(ev("payment.check", day, 10, _check_payment(sc, home), area="money"))
        elif p == "late" and day.day == 17 + (home.number % 8) and r < 0.85:
            out.append(ev("payment.late_check", day, 11, _check_payment(sc, home), area="money"))
        elif p == "online" and day.day == 3 + (home.number % 8) and home.owners:
            out.append(ev("payment.online", day, 19, _online_payment(sc, home, rng), area="money"))
        elif p == "autopay" and m == 0 and day.day == 5 + (home.number % 10) and home.owners:
            out.append(ev("autopay.enrol", day, 20, _autopay_enrol(sc, home), area="money"))
        elif p == "plan" and day.day == 1 + (home.number % 9):
            out.append(
                ev(
                    "payment.check",
                    day,
                    10,
                    _check_payment(sc, home, setup.MONTHLY_CENTS),
                    area="money",
                )
            )
        if p in ("never", "plan", "paid_off") and m >= 2 and day.day == 12:
            out.append(ev("collections.step", day, 13, _collections(sc, home), area="money"))
    # Homes on a payment plan pay its installments.
    if day.day in (9, 24):
        out.append(ev("payment_plan.installment", day, 10, _plan_installments(sc), area="money"))
    # --- the manager's desk -----------------------------------------------------------------
    if day.weekday() in (1, 4):  # Tuesday, Friday
        out.append(ev("deposit.made", day, 15, _deposit(sc), area="money", staff=[w.manager]))
    if day.weekday() == 0:  # Monday: Stripe pays out the week's online payments
        out.append(ev("stripe.payout", day, 9, _stripe_payout(sc), area="money", audit=False))
    out.append(ev("bank.review", day, 16, _bank_review(sc), area="money"))
    # --- the bank's own lines ---------------------------------------------------------------
    if day.day == 5:
        out.append(ev("bank.service_charge", day, 7, _bank_fee(sc), area="bank", audit=False))
    if day.day == 15:
        out.append(
            ev("bank.reserve_transfer", day, 12, _reserve_transfer(sc), area="bank", audit=False)
        )
    nxt = day + datetime.timedelta(days=1)
    if nxt.day == 1:
        out.append(ev("bank.interest", day, 18, _bank_interest(sc), area="bank", audit=False))
    out.append(ev("bank.checks_cashed", day, 8, _checks_cashed(sc, rng), area="bank", audit=False))
    # --- bills ------------------------------------------------------------------------------
    if day.day == 2:
        out.append(ev("invoice.contract", day, 11, _contract_invoices(sc), area="money"))
    if day.day == 20 and m % 3 == 1:
        out.append(
            ev(
                "invoice.pond",
                day,
                11,
                _invoice(sc, "Pond Pros (sim)", "5100", 72_000, rng),
                area="money",
            )
        )
    if day.month in (12, 1, 2, 3) and day.day in (8, 22):
        out.append(
            ev(
                "invoice.snow",
                day,
                11,
                _invoice(sc, "Creekside Snow (sim)", "5700", 48_000, rng),
                area="money",
            )
        )
    if day.day == 25 and m % 4 == 2:
        out.append(
            ev(
                "invoice.repairs",
                day,
                11,
                _invoice(sc, "Pond Pros (sim)", "5800", 23_500, rng),
                area="money",
            )
        )
    if day.day == 4:
        out.append(ev("invoice.approve", day, 18, _approve_invoices(sc), area="money"))
    if day.day in (6, 21):
        out.append(ev("invoice.approve", day, 18, _approve_invoices(sc), area="money"))
    if day.weekday() == 2:  # Wednesday: the treasurer writes the checks
        out.append(ev("vendor_check.issue", day, 17, _issue_checks(sc), area="money"))
    # --- reimbursements -----------------------------------------------------------------------
    if day.day == 9 and m % 2 == 1:
        out.append(
            ev("reimbursement.submit", day, 20, _reimbursement_submit(sc, rng), area="money")
        )
    if day.day == 13:
        out.append(ev("reimbursement.approve", day, 19, _reimbursement_approve(sc), area="money"))
    if day.day == 16:
        out.append(ev("reimbursement.pay", day, 19, _reimbursement_pay(sc), area="money"))
    # --- once in the run ------------------------------------------------------------------------
    inject_month = max(1, min(7, sc.o.months - 2))
    if m == inject_month and day.day == 20:
        out.append(ev("close.inject_problem", day, 7, _inject(sc, day), area="bank", audit=False))
    if m == inject_month + 1 and day.day == 6:
        out.append(ev("close.fix_problem", day, 10, _fix_injected(sc), area="money"))
    if day.month == 10 and day.day == 1:
        out.append(ev("budget.prepare_next", day, 18, _prepare_budget(sc, day), area="money"))
    if day.month == 11 and day.day == 1:
        out.append(ev("budget.notice", day, 18, _budget_notice(sc, day), area="money"))
    if day.month == 12 and day.day == 10:
        out.append(ev("budget.adopt", day, 18, _adopt_budget(sc, day), area="money"))
    if sc.notes.get("special_vote_passed") and not sc.notes.get("special_levied"):
        out.append(ev("special_assessment.levy", day, 18, _levy(sc, day), area="money"))
    if m >= 1 and day.day == 26 and m % 6 == 3:
        out.append(ev("reserve.spend", day, 11, _reserve_spend(sc, rng), area="money"))
    if day.month == 1 and day.day == 20:
        out.append(ev("tax.year_end_documents", day, 18, _tax_documents(sc, day), area="money"))
    if m >= 2 and day.day == 28 and m % 5 == 2:
        out.append(ev("cashflow.forecast", day, 18, _forecast(sc), area="money", audit=False))
    return out


# --- payments --------------------------------------------------------------------------------


def _check_payment(sc, home, cents=None):
    def run():
        today = timezone.localdate()
        owed = due_now(home, today)
        if owed <= 0:
            return SKIP
        if cents:
            owed = min(owed, cents)
        lot = _lot(home)
        actor = sc.world.staff_for(lot, prefer_manager=True)
        from ledger import services as ledger

        sc.notes.setdefault("check_no", 1000)
        sc.notes["check_no"] += 1
        ledger.record_payment(
            lot=lot,
            amount_cents=owed,
            received_on=today,
            reference=f"{sc.notes['check_no']}",
            actor=actor,
        )
        return f"lot {home.number} paid {owed}"

    return run


def _client(user):
    """A signed-in browser for ``user`` with a fresh re-authentication (the
    money set-up pages ask for one), driving the real pages in-process."""
    from django.test import Client

    c = Client()
    c.force_login(user)
    session = c.session
    session["account_authentication_methods_user"] = str(user.pk)
    session["account_authentication_methods"] = [
        {"method": "mfa", "type": "totp", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()
    return c


def _online_payment(sc, home, rng):
    method = "credit" if rng.random() < 0.5 else "bank"

    def run():
        from django.urls import reverse

        from payments.models import PendingCheckout

        today = timezone.localdate()
        if due_now(home, today) <= 0:
            return SKIP
        from ledger import accounts, allocation

        with system_access("simulation lookup"):
            charges = [
                str(ch.entry.pk)
                for ch in allocation.open_charges(home.lot_id, account=accounts.CURRENT)
            ]
        if not charges:
            return SKIP
        c = _client(home.owners[0])
        resp = c.post(
            reverse("pay_checkout", args=[home.lot_id]), {"method": method, "charge": charges}
        )
        if resp.status_code != 302 or "checkout.stripe.com" not in resp.get("Location", ""):
            raise RuntimeError(f"pay_checkout answered {resp.status_code}")
        with system_access("simulation lookup"):
            checkout = PendingCheckout.objects.latest("id")
        fake = sc.fakes.stripe
        if method == "bank":
            pi, _ch = fake.pay(checkout.session_id, bank=True, status="processing")
            sc.notes.setdefault("ach", []).append((pi, today + datetime.timedelta(days=3)))
        else:
            fake.pay(checkout.session_id, funding="credit")
        back = c.get(reverse("pay_return", args=[checkout.pk]), {"session_id": checkout.session_id})
        if back.status_code not in (200, 302):
            raise RuntimeError(f"pay_return answered {back.status_code}")
        return f"lot {home.number} paid online by {method}"

    return run


def _autopay_enrol(sc, home):
    def run():
        from django.urls import reverse

        from payments.models import AutopaySetup

        c = _client(home.owners[0])
        resp = c.post(reverse("autopay_start", args=[home.lot_id]), {"method": "us_bank_account"})
        if resp.status_code != 302:
            resp = c.post(reverse("autopay_start", args=[home.lot_id]), {"method": "card"})
        if resp.status_code != 302 or "checkout.stripe.com" not in resp.get("Location", ""):
            raise RuntimeError(f"autopay_start answered {resp.status_code}")
        with system_access("simulation lookup"):
            s = AutopaySetup.objects.latest("id")
        sc.fakes.stripe.complete_setup(s.session_id, funding="debit")
        back = c.get(reverse("autopay_return", args=[s.pk]), {"session_id": s.session_id})
        if back.status_code != 302:
            raise RuntimeError(f"autopay_return answered {back.status_code}")
        return f"lot {home.number} enrolled"

    return run


def settle_ach(sc):
    """Bank payments started three days ago settle at Stripe (the daily sync reads them)."""
    today = timezone.localdate()
    keep = []
    for pi, on in sc.notes.get("ach", []):
        if on <= today:
            sc.fakes.stripe.settle_ach(pi, ok=True)
        else:
            keep.append((pi, on))
    sc.notes["ach"] = keep


def _deposit(sc):
    def run():
        from ledger import accounting

        today = timezone.localdate()
        settle_ach(sc)
        with system_access("simulation lookup"):
            entries = [e for e in accounting.undeposited_payments() if e.posted_on <= today]
        if not entries:
            return SKIP
        actor = sc.world.manager or sc.world.board[1]
        n = sc.notes.setdefault("deposit_no", 0) + 1
        sc.notes["deposit_no"] = n
        batch = accounting.create_deposit(
            entry_ids=[e.pk for e in entries],
            deposited_on=today,
            bank_reference=f"D{n:04d}",
            actor=actor,
        )
        bank = sc.fakes.bank
        bank.post(
            bank.operating,
            today + datetime.timedelta(days=1),
            batch.total_cents,
            f"DEPOSIT D{n:04d}",
            "deposit",
        )
        return f"deposit of {len(entries)} payment(s), {batch.total_cents}"

    return run


def _stripe_payout(sc):
    def run():
        settle_ach(sc)
        po, net = sc.fakes.stripe.payout_pending()
        if po is None or net <= 0:
            return SKIP
        bank = sc.fakes.bank
        bank.post(bank.operating, timezone.localdate(), net, "STRIPE TRANSFER", "transfer")
        return f"payout {net}"

    return run


def _bank_fee(sc):
    def run():
        bank = sc.fakes.bank
        bank.post(bank.operating, timezone.localdate(), -1200, "MONTHLY SERVICE CHARGE", "fee")
        return "bank fee"

    return run


def _reserve_transfer(sc):
    def run():
        bank = sc.fakes.bank
        today = timezone.localdate()
        amount = setup.RESERVE_MONTHLY_CENTS
        bank.post(bank.operating, today, -amount, "ONLINE TRANSFER TO SAVINGS", "transfer")
        bank.post(bank.reserve, today, amount, "TRANSFER FROM CHECKING", "transfer")
        return "reserve transfer"

    return run


def _bank_interest(sc):
    def run():
        bank = sc.fakes.bank
        cents = max(1, bank.reserve.balance_through(timezone.localdate()) * 15 // 10000 // 12)
        bank.post(bank.reserve, timezone.localdate(), cents, "INTEREST PAID", "interest")
        return "interest"

    return run


def _checks_cashed(sc, rng):
    """Payees cash the association's checks 3 to 12 days after they are written."""

    def run():
        from operations.models import VendorCheck

        today = timezone.localdate()
        cashed = sc.notes.setdefault("cashed", set())
        bank = sc.fakes.bank
        n = 0
        with system_access("simulation lookup"):
            checks = list(VendorCheck.objects.filter(issued_on__lte=today))
        for c in checks:
            if c.pk in cashed or c.number in sc.notes.get("voided_checks", set()):
                continue
            wait = 3 + (int(c.number) % 10 if c.number.isdigit() else 5)
            if c.issued_on + datetime.timedelta(days=wait) > today:
                continue
            account = (
                bank.reserve
                if c.invoice_id in sc.notes.get("reserve_invoices", set())
                else bank.operating
            )
            bank.post(account, today, -c.amount_cents, f"CHECK {c.number}", "check")
            cashed.add(c.pk)
            n += 1
        for line in sc.notes.pop("transfers_out", []):
            bank.post(bank.operating, today, -line[1], f"ONLINE TRANSFER {line[0]}", "transfer")
            n += 1
        return f"{n} line(s)" if n else SKIP

    return run


def _bank_review(sc):
    """The treasurer looks at the bank feed: lines with exactly one fitting item
    the automatic match left (a tie between equal amounts) are matched by
    their reference, as a person reading the description would."""

    def run():
        from ledger.bankfeeds import matching
        from ledger.bankfeeds.base import BankFeedError
        from ledger.books import coding

        actor = sc.world.board[1] if len(sc.world.board) > 1 else sc.world.board[0]
        with system_access("simulation lookup"):
            lines = coding.unhandled_lines()
        if not lines:
            return SKIP
        done = 0
        for row in matching.suggestions(lines):
            line, cands = row["line"], row["candidates"]
            if not cands:
                continue
            pick = None
            desc = line.description or ""
            for c in cands:
                label = c.label or ""
                if desc.split()[-1:] and desc.split()[-1] in label:
                    pick = c
                    break
            pick = pick or cands[0]
            try:
                matching.confirm(line, kind=pick.kind, object_id=pick.object_id, actor=actor)
                done += 1
            except BankFeedError:  # taken by an earlier line in this review
                continue
        return f"{done} matched by hand" if done else SKIP

    return run


# --- invoices and checks -----------------------------------------------------------------------


def _vendor(name):
    from operations.models import Vendor

    with system_access("simulation lookup"):
        return Vendor.objects.get(name=name)


def _contract_invoices(sc):
    def run():
        from operations import services as ops
        from operations.models import Contract

        today = timezone.localdate()
        actor = sc.world.manager
        made = 0
        with system_access("simulation lookup"):
            contracts = list(Contract.objects.filter(pk__in=sc.notes.get("contracts", [])))
        for c in contracts:
            if c.state not in ("active", "approved") or (c.ends_on and c.ends_on < today):
                continue
            if ops.contract_status(c, today)["this_period"] is not None:
                continue
            amount = c.amount_cap_cents - 500 * (today.month % 3)
            inv = ops.create_invoice(
                vendor=c.vendor,
                number=f"{c.pk}-{today:%Y%m}",
                invoice_date=today,
                amount_cents=amount,
                category=c.category,
                description=f"{c.title} {today:%B %Y}",
                contract=c,
                actor=actor,
            )
            ops.INVOICE.apply(inv, "submit", actor=actor)
            made += 1
        return f"{made} contract invoice(s)" if made else SKIP

    return run


def _invoice(sc, vendor_name, code, cents, rng):
    def run():
        from operations import services as ops

        today = timezone.localdate()
        actor = sc.world.manager
        cats = setup._cats()
        amount = cents + int(rng.random() * 5000)
        inv = ops.create_invoice(
            vendor=_vendor(vendor_name),
            number=f"{vendor_name.split()[0][:4].upper()}-{today:%y%m%d}",
            invoice_date=today,
            amount_cents=amount,
            category=cats[code],
            description=f"{cats[code].name} (simulation)",
            actor=actor,
        )
        ops.INVOICE.apply(inv, "submit", actor=actor)
        return f"invoice {amount}"

    return run


def _approve_invoices(sc):
    def run():
        from operations import services as ops
        from operations.models import Invoice

        with system_access("simulation lookup"):
            waiting = list(Invoice.objects.filter(state="pending").order_by("pk"))
        if not waiting:
            return SKIP
        done = 0
        for inv in waiting:
            used = set()
            for _ in range(ops.approvals_required(inv)):
                actor = sc.world.staff_for(
                    inv, not_users=used | {inv.entered_by} if hasattr(inv, "entered_by") else used
                )
                if actor is None:
                    break
                inv = ops.approve(inv, note="Approved (simulation)", actor=actor)
                used.add(actor)
                if inv.state != "pending":
                    break
            done += 1
        return f"{done} invoice(s) approved"

    return run


def _issue_checks(sc):
    def run():
        from ledger import check_numbers
        from operations import vendor_checks

        today = timezone.localdate()
        actor = sc.world.board[1] if len(sc.world.board) > 1 else sc.world.board[0]
        with system_access("simulation lookup"):
            rows = vendor_checks.payables(actor)
        # Reimbursements are paid from their claim (D77), not by the check run.
        ids = [r.invoice.pk for r in rows if not r.refusal and r.claim is None]
        if not ids:
            return SKIP
        first = check_numbers.suggest_next() or "5001"
        made = vendor_checks.issue(ids, first_number=first, issued_on=today, actor=actor)
        return f"{len(made)} check(s) from #{first}"

    return run


# --- reimbursements ----------------------------------------------------------------------------


def _reimbursement_submit(sc, rng):
    def run():
        from operations import reimbursements as rb

        cats = setup._cats()
        board = sc.world.board
        # Board and committee work (D95): now and then the social committee's chair.
        committee = _committee_logins(sc)
        pool = board + committee[:1]
        who = pool[int(rng.random() * len(pool))]
        amount = 1500 + int(rng.random() * 70000)
        claim = rb.submit(
            amount_cents=amount,
            spent_on=timezone.localdate() - datetime.timedelta(days=3),
            payee="Hardware store (sim)",
            category=cats["5800"],
            description="Supplies for the pavilion (simulation)",
            files=[_receipt()],
            actor=who,
        )
        sc.notes.setdefault("claims", []).append(claim.pk)
        return f"claim {amount}"

    return run


def _committee_logins(sc):
    from governance.models import CommitteeMembership

    if "committee" not in sc.notes:
        return []
    with system_access("simulation lookup"):
        people = [
            m.person
            for m in CommitteeMembership.objects.filter(committee_id=sc.notes["committee"])
            .select_related("person")
            .order_by("pk")
        ]
    return [u for u in (sc.world.login(p) for p in people) if u is not None]


def _claims(status):
    from operations import reimbursements as rb
    from operations.models_payouts import ReimbursementClaim

    with system_access("simulation lookup"):
        claims = list(ReimbursementClaim.objects.order_by("pk"))
    return [c for c in claims if rb.state(c)["status"] == status]


def _reimbursement_approve(sc):
    def run():
        from operations import reimbursements as rb

        done = 0
        for claim in _claims("awaiting"):
            used = {claim.submitted_by}
            for _ in range(claim.required_approvals):
                actor = next((u for u in sc.world.board if u not in used), None)
                if actor is None:
                    break
                rb.approve(claim, note="OK (simulation)", actor=actor)
                used.add(actor)
                if rb.state(claim)["status"] != "awaiting":
                    break
            done += 1
        return f"{done} approved" if done else SKIP

    return run


def _reimbursement_pay(sc):
    def run():
        from operations import reimbursements as rb

        done = 0
        for claim in _claims("approved"):
            payer = next((u for u in sc.world.board if rb.payment_refusal(u, claim) is None), None)
            if payer is None:
                continue
            ref = f"R{claim.pk:04d}"
            rb.pay_by_transfer(claim, paid_on=timezone.localdate(), reference=ref, actor=payer)
            sc.notes.setdefault("transfers_out", []).append((ref, claim.amount_cents))
            inv = rb.invoice_for(claim)
            if inv is not None:
                sc.notes.setdefault("claim_invoices", set()).add(inv.pk)
            done += 1
        return f"{done} paid" if done else SKIP

    return run


# --- collections, legal, payment plan -----------------------------------------------------------


def _open_case(lot):
    from ledger.models import CollectionCase

    with system_access("simulation lookup"):
        return CollectionCase.objects.filter(lot=lot).exclude(state="closed").first()


def _collections(sc, home):
    """A home that stopped paying: case, reminders, final notice, then either a
    payment plan (the first such home) or legal collection with a lien (the
    second), paid in full later and the lien released."""

    def run():
        from ledger import accounting, legal, payment_plans
        from ledger import services as ledger

        today = timezone.localdate()
        lot = _lot(home)
        w = sc.world
        case = _open_case(lot)
        owed = due_now(home, today)
        staff = w.staff_for(lot)
        if case is None:
            if owed < setup.MONTHLY_CENTS * 2 or home.profile != "never":
                return SKIP
            ledger.open_case(lot=lot, notes="Two months unpaid (simulation)", actor=staff)
            return f"case opened for lot {home.number}"
        nevers = sc.notes.setdefault("never_homes", [h.number for h in w.owner_homes("never")])
        first = bool(nevers) and home.number == nevers[0]
        if owed <= 0 and case.state in ("payment_plan", "open", "reminder_sent"):
            ledger.COLLECTION.apply(case, "close", actor=staff, note="Paid in full (simulation)")
            home.profile = "paid_off"
            return f"case closed for lot {home.number}"
        step = {
            "open": "send_reminder",
            "reminder_sent": "send_second_reminder",
            "second_reminder": "send_final_notice" if first else "refer",
        }.get(case.state)
        if case.state == "final_notice" and first and payment_plans.plan_for_case(case) is None:
            _charges, total, _acct = payment_plans.covered_charges(case)
            plan = payment_plans.prepare(
                case,
                installments=payment_plans.even_installments(
                    total, 4, today + datetime.timedelta(days=10)
                ),
                start_on=today,
                note="Agreed by phone (simulation).",
                actor=w.manager or staff,
            )
            approver = w.staff_for(lot, not_users={w.manager})
            payment_plans.approve(plan, note="Approved (simulation)", actor=approver)
            if home.owners:
                payment_plans.acknowledge(plan, actor=home.owners[0])
            sc.notes.setdefault("plans", []).append(case.pk)
            home.profile = "plan"  # keeps up with each month's charge from now on
            return f"payment plan for lot {home.number}"
        if step:
            ledger.COLLECTION.apply(case, step, actor=staff, note="Simulation step")
            if step != "refer":
                try:
                    accounting.generate_case_letter(case, actor=staff)
                except accounting.AccountingError as exc:
                    sc.notes.setdefault("letters_refused", []).append(str(exc)[:200])
            return f"{step} for lot {home.number}"
        if case.state == "referred":
            done = {e.kind for e in legal.events(case)}
            board = w.staff_for(lot, not_users={w.manager})
            if "referral" not in done:
                legal.record(case, kind="referral", firm="Fictional Law Office (sim)", actor=board)
                return "referred to counsel"
            if "lien" not in done:
                legal.record(case, kind="lien", lien_filing="statement", actor=board)
                cost = legal.record(
                    case,
                    kind="cost",
                    amount_cents=35_000,
                    detail="Lien filing and letter (simulation)",
                    chargeable=True,
                    actor=board,
                )
                legal.post_cost(cost, actor=w.staff_for(lot, not_users={w.manager, board}) or board)
                return "lien recorded, cost charged"
            if "release" not in done and owed > 0 and sc.month_index(today) >= 6:
                # The owner pays everything at once (a refinance, say).
                ledger.record_payment(
                    lot=lot,
                    amount_cents=owed,
                    received_on=today,
                    reference=f"PAYOFF-{today:%y%m%d}",
                    actor=w.manager,
                )
                home.profile = "paid_off"  # pays each month on time from now on
                return f"paid in full {owed}"
            if "release" not in done and owed <= 0:
                legal.record(case, kind="release", actor=board)
                ledger.COLLECTION.apply(case, "close", actor=board, note="Paid; lien released")
                return "lien released, case closed"
        return SKIP

    return run


def _plan_installments(sc):
    def run():
        from ledger import payment_plans
        from ledger import services as ledger
        from ledger.models import CollectionCase

        today = timezone.localdate()
        paid = 0
        for case_id in sc.notes.get("plans", []):
            with system_access("simulation lookup"):
                case = CollectionCase.objects.get(pk=case_id)
            plan = payment_plans.plan_for_case(case)
            if plan is None or payment_plans.status(plan) != "active":
                continue
            soon = today + datetime.timedelta(days=5)
            rows = [
                r
                for r in payment_plans.schedule(plan, today)
                if r["status"] != "met" and r["installment"].due_on <= soon
            ]
            if not rows:
                continue
            left = rows[-1]["cumulative_cents"] - payment_plans.paid_cents(plan, today)
            if left <= 0:
                continue
            ledger.record_payment(
                lot=case.lot,
                amount_cents=left,
                received_on=today,
                reference=f"PLAN{case.pk}-{today:%m%d}",
                actor=sc.world.manager,
            )
            paid += 1
        return f"{paid} installment(s)" if paid else SKIP

    return run


# --- the injected problem -------------------------------------------------------------------------


def _inject(sc, day):
    def run():
        from ledger.books import periods

        bank = sc.fakes.bank
        bank.post(bank.operating, timezone.localdate(), -12_345, "WIRE OUT 77 (sim)", "wire")
        sc.injected_months[periods.month_key(day)] = "bank_lines"
        sc.notes["injected_month"] = periods.month_key(day)
        return "unexplained bank line"

    return run


def _fix_injected(sc):
    def run():
        from ledger.books import coding, funds

        key = sc.notes.get("injected_month")
        if not key:
            return SKIP
        actor = sc.world.board[1] if len(sc.world.board) > 1 else sc.world.board[0]
        with system_access("simulation lookup"):
            lines = [x for x in coding.unhandled_lines() if "WIRE OUT 77" in x.description]
        cats = {c.key: c for c in funds.active_categories()}
        for line in lines:
            coding.code_line(line, cats["adjustments"], actor=actor, memo="Bank error (simulation)")
        sc.injected_months[f"{key}:fixed"] = "yes"
        return f"{len(lines)} line(s) coded"

    return run


# --- budget, special assessment, reserve, tax ----------------------------------------------------


def _prepare_budget(sc, day):
    def run():
        from ledger import budget_prep
        from ledger.models import Budget

        with system_access("simulation lookup"):
            source = Budget.objects.filter(fiscal_year=day.year).first()
            if source is None or Budget.objects.filter(fiscal_year=day.year + 1).exists():
                return SKIP
        draft = budget_prep.prepare_next(
            source, basis="higher", increase_bp=300, actor=sc.world.board[1]
        )
        sc.notes["next_budget"] = draft.pk
        return f"draft FY{day.year + 1}"

    return run


def _budget_notice(sc, day):
    def run():
        from ledger import services as ledger
        from ledger.models import Budget

        with system_access("simulation lookup"):
            b = Budget.objects.filter(fiscal_year=day.year + 1).first()
        if b is None:
            return SKIP
        ledger.send_budget_notice(b, stage="proposed", actor=sc.world.board[0])
        return "notice sent"

    return run


def _adopt_budget(sc, day):
    def run():
        from ledger import services as ledger
        from ledger.models import Budget

        with system_access("simulation lookup"):
            b = Budget.objects.filter(fiscal_year=day.year + 1).first()
        if b is None:
            setup.budget(sc, sc.world.board[1], fiscal_year=day.year + 1)
            return "adopted (fresh)"
        if b.status == "adopted":
            return SKIP
        ledger.adopt_budget(b, actor=sc.world.board[0])
        return f"FY{day.year + 1} adopted"

    return run


def _levy(sc, day):
    def run():
        from governance.models import Vote
        from ledger import special_assessments as sa

        with system_access("simulation lookup"):
            vote = Vote.objects.get(pk=sc.notes["special_vote"])
        today = timezone.localdate()
        dues = [today.replace(day=1) + datetime.timedelta(days=32 * k + 40) for k in range(4)]
        dues = [d.replace(day=1) for d in dues]
        levy = sa.levy(
            title=sc.notes["special_title"],
            purpose="Repave the entrance road (simulation).",
            total_cents=sc.notes["special_total"],
            due_dates=dues,
            fund="reserve",
            resolution_ref="Resolution (simulation)",
            vote=vote,
            actor=sc.world.board[0],
        )
        sa.send_notice(levy, actor=sc.world.board[0])
        sc.notes["special_levied"] = levy.pk
        return f"levied {levy.total_cents} in 4 installments"

    return run


def _reserve_spend(sc, rng):
    """A reserve component replaced: invoice approved by two, paid by check from
    the reserve account."""

    def run():
        from operations import services as ops

        today = timezone.localdate()
        cats = setup._cats()
        inv = ops.create_invoice(
            vendor=_vendor("Pond Pros (sim)"),
            number=f"RES-{today:%y%m%d}",
            invoice_date=today,
            amount_cents=180_000 + int(rng.random() * 20_000),
            category=cats["5800"],
            description="Entrance lighting replacement (reserve component, simulation)",
            actor=sc.world.manager,
        )
        ops.INVOICE.apply(inv, "submit", actor=sc.world.manager)
        sc.notes.setdefault("reserve_invoices", set()).add(inv.pk)
        return f"reserve invoice {inv.amount_cents}"

    return run


def _tax_documents(sc, day):
    def run():
        from ledger.books import tax

        fy = day.year - 1
        if fy < sc.o.start.year:
            return SKIP
        w = tax.form_1120h(fy)
        tax.ensure_deadlines(actor=sc.world.board[1], fiscal_year=fy)
        tax.worksheet_csv(fy, actor=sc.world.board[1])
        tax.iris_csv(fy, actor=sc.world.board[1])
        sc.notes.setdefault("tax", {})[fy] = {
            "sixty": w["sixty"]["passes"],
            "ninety": w["ninety"]["passes"] if "ninety" in w else None,
        }
        return f"FY{fy} worksheets"

    return run


def _forecast(sc):
    def run():
        from ledger import cashflow

        cashflow.build()
        return SKIP

    return run


# --- year end figures for the report -------------------------------------------------------------


def year_end_figures(sc) -> dict:
    from ledger.books import periods, statements
    from ledger.books.models import PeriodClose, YearEndPackage

    out = {"years": {}, "months_closed": 0}
    out["months_closed"] = PeriodClose.objects.filter(kind="month").count()
    for pkg in YearEndPackage.objects.order_by("fiscal_year", "id"):
        _s, end = periods.year_bounds(pkg.fiscal_year)
        bs = statements.balance_sheet(end)
        income = statements.income_statement(*periods.year_bounds(pkg.fiscal_year))
        out["years"][str(pkg.fiscal_year)] = {
            "package_files": pkg.file_count,
            "year_closed": pkg.year_closed,
            "automatic": pkg.automatic,
            "funds": {
                f: {
                    "assets": v["assets"],
                    "liabilities": v["liabilities"],
                    "fund_balance": v["fund_balance"],
                    "net_income": v["net_income"],
                }
                for f, v in bs["funds"].items()
            },
            "income_total": income.get("income_total") if isinstance(income, dict) else None,
            "expense_total": income.get("expense_total") if isinstance(income, dict) else None,
            "tax": sc.notes.get("tax", {}).get(pkg.fiscal_year),
        }
    return out

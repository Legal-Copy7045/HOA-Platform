"""Special assessments (D75), payment plans (D76) and budget preparation (D78)."""

import datetime
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.db_roles import append_only_tables
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation, budget_prep, payment_plans, services
from ledger import special_assessments as sa_service
from ledger.models import (
    Budget,
    CollectionCase,
    EntryKind,
    LedgerEntry,
    PaymentPlanEvent,
    SpecialAssessment,
    SpecialAssessmentCharge,
)

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
D = datetime.timedelta


def _rule(key, value, citation="Declaration §8 (test)"):
    community.set_rule(key=key, value=value, citation=citation, actor=SYSTEM)


@pytest.fixture
def w():
    lots = [LotFactory(street_address=f"{n} Alpha Way") for n in (1, 2, 3)]
    owners = [make_member(lots=[lot]) for lot in lots]
    board = make_member(board=True)
    board2 = make_member(board=True)
    board_owner = make_member(board=True, lots=[LotFactory(street_address="4 Bravo Way")])
    manager = make_member(roles=[Role.MANAGER])
    return {
        "lots": lots,
        "owners": owners,
        "board": board,
        "board2": board2,
        "board_owner": board_owner,
        "manager": manager,
    }


def _entries(**kw):
    with system_access("t"):
        return list(LedgerEntry.objects.filter(**kw).order_by("id"))


def _levy(w, total=40000, dates=None, actor=None, **kw):
    return sa_service.levy(
        title="Pond dredging",
        purpose="Dredge the pond",
        total_cents=total,
        due_dates=dates or [TODAY + D(days=10), TODAY + D(days=40)],
        resolution_ref="Resolution 2026-20",
        actor=actor or w["board"],
        **kw,
    )


# --- Special assessments ---------------------------------------------------------------------


def test_equal_split_and_installment_amounts():
    assert sa_service.installment_amounts(1001, 3) == [335, 333, 333]
    lots = [LotFactory(street_address=f"{n} Zed Road") for n in (2, 1, 3)]
    shares = sa_service.equal_shares(1000, sorted(lots, key=lambda x: x.sort_key))
    assert sum(shares.values()) == 1000 and sorted(shares.values()) == [333, 333, 334]


def test_levy_needs_resolution_and_board(w):
    with pytest.raises(sa_service.SpecialAssessmentError, match="resolution"):
        sa_service.levy(
            title="x",
            purpose="y",
            total_cents=100,
            due_dates=[TODAY + D(days=1)],
            actor=w["board"],
        )
    with pytest.raises(PermissionDenied):
        _levy(w, actor=w["manager"])
    with pytest.raises(PermissionDenied):
        _levy(w, actor=w["owners"][0])
    with pytest.raises(sa_service.SpecialAssessmentError, match="before today"):
        _levy(w, dates=[TODAY - D(days=1)])


def test_vote_required_above_threshold(w):
    _rule("special_assessment.vote_required_cents", "10000")
    _rule("special_assessment.vote_percent", "67")
    with pytest.raises(sa_service.SpecialAssessmentError, match="owners must approve"):
        _levy(w, total=20000)
    sa = _levy(w, total=20000, vote_ref="Owners' vote at the annual meeting")
    assert sa.vote_required and sa.vote_threshold_percent == 67
    assert _levy(w, total=5000).vote_required is False


def test_installments_post_once_on_due_date_and_allocate(w):
    _levy(w, total=40000)  # 4 homes: 10000 each, 5000 per installment
    assert not _entries(kind=EntryKind.SPECIAL_ASSESSMENT)
    assert sa_service.post_due(today=TODAY + D(days=10)) == 4
    assert sa_service.post_due(today=TODAY + D(days=10)) == 0  # idempotent
    rows = _entries(kind=EntryKind.SPECIAL_ASSESSMENT)
    assert {e.amount_cents for e in rows} == {5000}
    assert rows[0].description == "Pond dredging \N{EN DASH} installment 1 of 2"
    assert rows[0].posted_on == TODAY + D(days=10)
    # Paid by open-item allocation like any charge.
    lot = w["lots"][0]
    services.record_payment(
        lot=lot, amount_cents=5000, received_on=TODAY, reference="101", actor=w["board"]
    )
    charge = next(e for e in rows if e.lot_id == lot.pk)
    with system_access("t"):
        assert allocation.charge_status(charge) == "paid"
    assert sa_service.post_due(today=TODAY + D(days=60)) == 4
    assert len(_entries(kind=EntryKind.SPECIAL_ASSESSMENT)) == 8
    owner_view = sa_service.for_lot(lot.pk)
    assert [r["status"] for r in owner_view[0]["installments"]] == ["paid", "due"]


def test_custom_allocation(w):
    _rule("special_assessment.equal_shares", "false")
    lots = sa_service.homes()
    with pytest.raises(sa_service.SpecialAssessmentError, match="add up"):
        _levy(w, total=1000, custom={lots[0].pk: 500})
    sa = _levy(w, total=1000, custom={lots[0].pk: 700, lots[1].pk: 300})
    assert {s.lot_id: s.amount_cents for s in sa_service.shares(sa)} == {
        lots[0].pk: 700,
        lots[1].pk: 300,
    }


def test_cancel_future_installments(w):
    sa = _levy(w, dates=[TODAY + D(days=1), TODAY + D(days=30), TODAY + D(days=60)])
    sa_service.post_due(today=TODAY + D(days=1))
    with pytest.raises(sa_service.SpecialAssessmentError, match="already posted"):
        sa_service.cancel_future(sa, from_number=1, reason="x", actor=w["board"])
    with pytest.raises(PermissionDenied):
        sa_service.cancel_future(sa, from_number=2, reason="x", actor=w["manager"])
    sa_service.cancel_future(sa, from_number=2, reason="Grant received", actor=w["board"])
    assert sa_service.post_due(today=TODAY + D(days=90)) == 0
    assert [r["status"] for r in sa_service.installments(sa)] == [
        "posted",
        "cancelled",
        "cancelled",
    ]


def test_special_assessment_records_are_append_only(w):
    sa = _levy(w)
    with pytest.raises(AppendOnlyError):
        sa.save()
    tables = append_only_tables()
    for name in (
        "ledger_specialassessment",
        "ledger_specialassessmentcharge",
        "ledger_specialassessmentcancel",
        "ledger_paymentplan",
        "ledger_paymentplanevent",
    ):
        assert name in tables
    with pytest.raises(Exception), connection.cursor() as cur:  # noqa: B017
        cur.execute("UPDATE ledger_specialassessment SET total_cents = 1")


def test_notice_emailed_once_and_audited_without_personal_data(w):
    from community.models import ContactProfile, Person

    with system_access("t"):
        person = Person.objects.get(user=w["owners"][0])
        ContactProfile.objects.update_or_create(
            person=person, defaults={"electronic_notice_consent": True}
        )
    sa = _levy(w)
    with mock.patch("django.db.transaction.on_commit", lambda fn: fn()):
        result = sa_service.send_notice(sa, actor=w["board"])
    assert result["emailed"] >= 1
    assert any("Pond dredging" in m.subject for m in mail.outbox)
    with pytest.raises(sa_service.SpecialAssessmentError):
        sa_service.send_notice(sa, actor=w["board"])
    event = AuditEvent.objects.filter(action="special_assessment.notice_sent").get()
    assert set(event.metadata) <= {"assessment_id", "emailed", "paper"}


def test_late_fees_on_installments_only_when_levied_with_them(w, settings):
    settings.LEDGER_LATE_FEES_ENABLED = True
    _rule("late_fee.cents", "2500")
    _rule("late_fee.grace_days", "10")
    off = sa_service.levy(
        title="No fees",
        purpose="x",
        total_cents=4000,
        due_dates=[TODAY - D(days=30)],
        resolution_ref="R",
        actor=SYSTEM,
    )
    _rule("special_assessment.late_fees", "true")
    on = sa_service.levy(
        title="With fees",
        purpose="x",
        total_cents=4000,
        due_dates=[TODAY - D(days=30)],
        resolution_ref="R",
        actor=SYSTEM,
    )
    assert not off.late_fees and on.late_fees
    posted = sa_service.post_late_fees(actor=w["board_owner"])
    # Four homes; the board member's own home is left for another member.
    assert posted == 3
    assert sa_service.post_late_fees(actor=w["board"]) == 1
    assert sa_service.post_late_fees(actor=w["board"]) == 0


def test_scheduled_task_posts_due_installments(w):
    from ledger.tasks import special_assessments as task

    sa_service.levy(
        title="Paving",
        purpose="x",
        total_cents=400,
        due_dates=[TODAY + D(days=5)],
        resolution_ref="R",
        actor=w["board"],
    )
    assert task(timestamp=0) == 0
    with mock.patch("django.utils.timezone.localdate", return_value=TODAY + D(days=5)):
        assert task(timestamp=0) == 4
        assert task(timestamp=0) == 0


def test_special_assessment_pages(client, w):
    sa = _levy(w)
    client.force_login(w["board"])
    assert client.get(reverse("special_assessments")).status_code == 200
    assert (
        b"Pond dredging" in client.get(reverse("special_assessment_detail", args=[sa.pk])).content
    )
    page = client.get(reverse("special_assessment_new"))
    assert page.status_code == 200
    response = client.post(
        reverse("special_assessment_new"),
        {
            "title": "Gate",
            "purpose": "New gate",
            "total_cents": "400.00",
            "fund": "operating",
            "installments": "2",
            "first_due": (TODAY + D(days=3)).isoformat(),
            "every_months": "1",
            "resolution_ref": "Res 9",
        },
    )
    assert response.status_code == 302
    assert SpecialAssessment.objects.filter(title="Gate").exists()
    client.force_login(w["owners"][0])
    assert client.get(reverse("special_assessments")).status_code == 403
    page = client.get(reverse("my_account")).content.decode()
    assert "Special assessment: Pond dredging" in page


# --- Payment plans ----------------------------------------------------------------------------


def _arrears(w, lot=None, cents=30000):
    lot = lot or w["lots"][0]
    services.post_charge(
        lot=lot,
        kind="other_charge",
        amount_cents=cents,
        description="Arrears",
        posted_on=TODAY - D(days=90),
        actor=w["board"],
    )
    return services.open_case(lot=lot, actor=w["board"])


def _plan(w, case, actor=None, first=None):
    first = first or TODAY + D(days=5)
    return payment_plans.prepare(
        case,
        installments=[(first, 10000), (first + D(days=30), 10000), (first + D(days=60), 10000)],
        start_on=min(first, TODAY),
        actor=actor or w["manager"],
    )


def test_prepare_checks_total_and_two_people(w):
    case = _arrears(w)
    with pytest.raises(payment_plans.PaymentPlanError, match="add up"):
        payment_plans.prepare(case, installments=[(TODAY + D(days=5), 100)], actor=w["manager"])
    plan = _plan(w, case)
    assert plan.total_cents == 30000 and plan.prior_state == "open"
    with pytest.raises(payment_plans.PaymentPlanError, match="already has"):
        _plan(w, case)
    with pytest.raises(PermissionDenied):
        payment_plans.approve(plan, actor=w["manager"])  # the manager never approves
    plan_board = plan
    payment_plans.approve(plan_board, actor=w["board"])
    case.refresh_from_db()
    assert case.state == "payment_plan"
    assert payment_plans.status(plan) == "active"


def test_preparer_cannot_approve_and_own_matter_refused(w):
    case = _arrears(w, lot=w["lots"][1])
    plan = _plan(w, case, actor=w["board"])
    with pytest.raises(payment_plans.PaymentPlanError, match="another board member"):
        payment_plans.approve(plan, actor=w["board"])
    own_lot = LotFactory(street_address="9 Own Way")
    with system_access("t"):
        from community.models import Person

        person = Person.objects.get(user=w["board2"])
    community.record_ownership(
        person=person, lot=own_lot, start_date=TODAY - D(days=400), actor=SYSTEM
    )
    own_case = _arrears(w, lot=own_lot)
    own_plan = _plan(w, own_case)
    with pytest.raises(payment_plans.PaymentPlanError, match="own"):
        payment_plans.approve(own_plan, actor=w["board2"])
    assert AuditEvent.objects.filter(action="policy.own_matter_refused").exists()


def test_generic_case_step_cannot_skip_the_plan(w):
    from core.workflow import WorkflowError

    case = _arrears(w)
    with pytest.raises(WorkflowError):
        services.COLLECTION.apply(case, "payment_plan", actor=w["board"], note="6 months")
    with pytest.raises(WorkflowError):
        services.COLLECTION.apply(case, "plan_end_open", actor=w["board"], note="x")


def test_with_the_feature_off_the_note_only_step_works(w):
    from core.models import FeatureSetting

    FeatureSetting.objects.create(key="payment_plans", enabled=False)
    case = _arrears(w)
    services.COLLECTION.apply(case, "payment_plan", actor=w["board"], note="6 months")
    case.refresh_from_db()
    assert case.state == "payment_plan"


def test_plan_completes_and_closes_case(w):
    case = _arrears(w)
    plan = _plan(w, case)
    payment_plans.approve(plan, actor=w["board"])
    services.record_payment(
        lot=w["lots"][0], amount_cents=10000, received_on=TODAY, reference="1", actor=w["board"]
    )
    assert payment_plans.schedule(plan)[0]["status"] == "met"
    assert payment_plans.check_plans() == {"completed": 0, "defaulted": 0}
    services.record_payment(
        lot=w["lots"][0], amount_cents=20000, received_on=TODAY, reference="2", actor=w["board"]
    )
    assert payment_plans.check_plans()["completed"] == 1
    case.refresh_from_db()
    assert case.state == "closed"
    assert payment_plans.status(plan) == "completed"
    assert payment_plans.check_plans()["completed"] == 0  # once


def test_plan_defaults_back_to_prior_state(w):
    case = _arrears(w)
    services.COLLECTION.apply(case, "send_reminder", actor=w["board"])
    plan = _plan(w, case, first=TODAY - D(days=50))
    payment_plans.approve(plan, actor=w["board"])
    rows = payment_plans.schedule(plan)
    assert rows[0]["status"] == "missed"
    items = payment_plans.board_items(w["board2"])
    assert any("missed" in i.title for i in items)
    assert w["lots"][0].pk not in payment_plans.late_fee_suspended_lots()
    assert payment_plans.check_plans()["defaulted"] == 1
    case.refresh_from_db()
    assert case.state == "reminder_sent"
    assert payment_plans.status(plan) == "defaulted"
    event = AuditEvent.objects.filter(action="payment_plan.defaulted").get()
    assert set(event.metadata) <= {"plan_id", "case_id", "lot_id", "missed"}
    with system_access("t"):
        assert PaymentPlanEvent.objects.filter(plan=plan, kind="defaulted").count() == 1


def test_late_fees_suspended_while_plan_is_current(w, settings):
    settings.LEDGER_LATE_FEES_ENABLED = True
    case = _arrears(w)
    plan = _plan(w, case)
    payment_plans.approve(plan, actor=w["board"])
    assert w["lots"][0].pk in payment_plans.late_fee_suspended_lots()
    _rule("payment_plan.suspend_late_fees", "false")
    case2 = _arrears(w, lot=w["lots"][1])
    plan2 = _plan(w, case2)
    payment_plans.approve(plan2, actor=w["board"])
    assert w["lots"][1].pk not in payment_plans.late_fee_suspended_lots()


def test_owner_acknowledges_and_sees_plan(client, w):
    case = _arrears(w)
    plan = _plan(w, case)
    payment_plans.approve(plan, actor=w["board"])
    other = w["owners"][1]
    with pytest.raises(payment_plans.PaymentPlanError):
        payment_plans.acknowledge(plan, actor=other)
    client.force_login(w["owners"][0])
    page = client.get(reverse("my_account")).content.decode()
    assert "Your payment plan" in page and "Next installment" in page
    assert client.post(reverse("payment_plan_acknowledge", args=[plan.pk])).status_code == 302
    assert payment_plans.overview(plan)["acknowledged"] is not None
    letter = client.get(reverse("payment_plan_letter", args=[plan.pk]))
    assert letter.status_code == 200 and letter["Content-Type"] == "application/pdf"
    client.force_login(other)
    assert client.get(reverse("payment_plan_letter", args=[plan.pk])).status_code == 404
    assert client.post(reverse("payment_plan_acknowledge", args=[plan.pk])).status_code == 404
    assert client.get(reverse("payment_plan_detail", args=[plan.pk])).status_code == 403


def test_payment_plan_pages(client, w):
    case = _arrears(w)
    client.force_login(w["manager"])
    page = client.get(reverse("collection_detail", args=[case.pk])).content.decode()
    assert reverse("payment_plan_new", args=[case.pk]) in page
    assert reverse("collection_action", args=[case.pk, "payment_plan"]) not in page
    response = client.post(
        reverse("payment_plan_new", args=[case.pk]),
        {"count": "3", "first_due": (TODAY + D(days=5)).isoformat()},
    )
    assert response.status_code == 302
    with system_access("t"):
        plan = case.plans.get()
    client.force_login(w["board"])
    assert client.post(reverse("payment_plan_action", args=[plan.pk, "approve"])).status_code == 302
    with system_access("t"):
        assert CollectionCase.objects.get(pk=case.pk).state == "payment_plan"
    assert client.get(reverse("payment_plans")).status_code == 200


# --- Budget preparation -----------------------------------------------------------------------


def _budget(w, year=2026):
    from operations.models import ExpenseCategory

    landscaping = ExpenseCategory.objects.create(code="5200", name="Landscaping")
    insurance = ExpenseCategory.objects.create(code="5300", name="Insurance")
    budget = services.create_budget(fiscal_year=year, actor=w["board"])
    services.add_line(
        budget,
        kind="expense",
        name="Landscaping",
        annual_cents=1200000,
        category=landscaping,
        actor=w["board"],
    )
    services.add_line(
        budget,
        kind="expense",
        name="Insurance",
        annual_cents=600000,
        category=insurance,
        actor=w["board"],
    )
    services.add_line(
        budget,
        kind="income",
        name="Resale fees",
        annual_cents=20000,
        income_source="resale_fees",
        actor=w["board"],
    )
    return budget, landscaping, insurance


def test_grow_rounds_half_up():
    assert budget_prep.grow(10000, 250) == 10250
    assert budget_prep.grow(333, 150) == 338  # 337.995 -> 338


def test_prepare_next_year_from_budget_and_actuals(w):
    from operations import services as ops
    from operations.models import Vendor

    budget, landscaping, _insurance = _budget(w)
    vendor = Vendor.objects.create(name="Green Co")
    inv = ops.create_invoice(
        vendor=vendor,
        number="A1",
        invoice_date=datetime.date(2026, 3, 1),
        amount_cents=1500000,
        category=landscaping,
        actor=w["manager"],
    )
    ops.INVOICE.apply(inv, "submit", actor=w["manager"])
    ops.approve(inv, actor=w["board"])
    ops.approve(inv, actor=w["board2"])
    _rule("dues.monthly_cents", "3000")
    new = budget_prep.prepare_next(budget, basis="higher", increase_bp=0, actor=w["board"])
    assert new.fiscal_year == 2027 and new.status == "draft"
    lines = {line.name: line.annual_cents for line in new.lines.all()}
    assert lines["Landscaping"] == 1500000  # actual above the budget
    assert lines["Insurance"] == 600000
    ph = budget_prep.per_home(new)
    # (1,500,000 + 600,000 - 20,000) / 4 homes / 12 = 43,333.33 -> 43,334 -> $434.00
    assert ph["homes"] == 4 and ph["exact_cents"] == 43334 and ph["monthly_cents"] == 43400
    assert ph["change_cents"] == 43400 - 3000
    assert lines["Assessments"] == 43400 * 4 * 12
    with pytest.raises(budget_prep.BudgetPrepError, match="already"):
        budget_prep.prepare_next(budget, actor=w["board"])
    with pytest.raises(PermissionDenied):
        budget_prep.prepare_next(budget, actor=w["manager"])
    comp = budget_prep.comparison(new)
    row = next(r for r in comp["rows"] if r["line"].name == "Landscaping")
    assert row["prior_cents"] == 1200000 and row["actual_cents"] == 1500000
    assert row["change_cents"] == 300000


def test_prepare_uses_adopted_reserve_plan(w):
    from ledger import reserve_plan

    budget, *_ = _budget(w)
    with mock.patch.object(
        reserve_plan,
        "latest_snapshot",
        return_value=mock.Mock(annual_contribution_cents=480000, needed_contribution_cents=1),
    ):
        new = budget_prep.prepare_next(budget, actor=w["board"])
    assert new.lines.get(name="Reserve contribution").annual_cents == 480000


def test_budget_prepare_pages(client, w):
    budget, *_ = _budget(w)
    client.force_login(w["board"])
    page = client.get(reverse("budgets")).content.decode()
    assert "Start next year" in page
    response = client.post(
        reverse("budget_prepare"), {"source": budget.pk, "basis": "budget", "increase": "3"}
    )
    new = Budget.objects.get(fiscal_year=2027)
    assert response.status_code == 302
    assert new.lines.get(name="Landscaping").annual_cents == 1236000
    page = client.get(reverse("budget_compare", args=[new.pk])).content.decode()
    assert "per home" in page
    client.force_login(w["owners"][0])
    assert client.get(reverse("budget_compare", args=[new.pk])).status_code == 403


def test_charge_kind_in_allocation_and_journal(w):
    from ledger import accounting

    _levy(w, dates=[TODAY])
    rows = accounting.journal_rows(TODAY, TODAY)
    assert any("Special assessment" in r[6] for r in rows)
    # Its own income account (books branch: map to the assessment's fund).
    assert any(r[3] == "4400 Special assessment income" and r[5] for r in rows)
    with system_access("t"):
        assert SpecialAssessmentCharge.objects.count() == 4


# --- Demo data --------------------------------------------------------------------------------


def test_finance_gaps_demo_seed(w):
    from ledger import demo_finance_gaps
    from ledger.models import PaymentPlan
    from operations import reimbursements as rb
    from operations import vendor_checks as vc
    from operations.models import ReimbursementClaim, VendorCheck

    make_member(board=True)
    from community.models import Lot

    for n in (5, 19, 33):
        if not Lot.objects.unscoped("t").filter(number=n).exists():
            make_member(lots=[LotFactory(number=n, street_address=f"{n} Demo Lane")])
    _budget(w, year=TODAY.year)
    out = demo_finance_gaps.seed()
    assert out["special"] and out["plans"] == 2 and out["claims"] == 4 and out["checks"] == 3
    with system_access("t"):
        states = {rb.state(c)["status"] for c in ReimbursementClaim.objects.all()}
        plans = sorted(payment_plans.status(p) for p in PaymentPlan.objects.all())
    assert states == {"awaiting", "approved", "paid", "declined"}
    assert plans == ["active", "defaulted"]
    assert {vc.status(c) for c in VendorCheck.objects.all()} == {"issued", "cleared", "voided"}
    assert Budget.objects.filter(fiscal_year=TODAY.year + 1).exists()
    assert demo_finance_gaps.seed() == {}  # once

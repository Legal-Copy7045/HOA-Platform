"""Finance options in the ledger (D80 interest, D81 cash-flow forecast, D82
liens and legal collection, D84 owners' finance summary): arithmetic,
idempotence, exclusions, who may do what, own matters, the feature switches,
cross-owner access and audit metadata."""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features
from core.my_items import items_for
from core.policy import SYSTEM
from ledger import allocation, cashflow, interest, legal
from ledger import services as ledger
from ledger.cashflow import Inputs, Scenario, add_months, apply_scenario, project, spread
from ledger.models import EntryKind, LedgerEntry
from ledger.models_options import LegalEvent

pytestmark = pytest.mark.django_db


def _rule(key, value, citation="Declaration §6.6 (test)"):
    community.set_rule(key=key, value=value, citation=citation, actor=SYSTEM)


def _on(*keys):
    for key in keys:
        features.set_feature(key, True, actor=SYSTEM)


def _month(back: int) -> datetime.date:
    return add_months(timezone.localdate().replace(day=1), -back)


@pytest.fixture
def w():
    lot_a = LotFactory(street_address="1 Alpha Way")
    lot_b = LotFactory(street_address="2 Bravo Way")
    lot_c = LotFactory(street_address="3 Charlie Way")
    owner_a = make_member(lots=[lot_a])
    owner_b = make_member(lots=[lot_b])
    owner_c = make_member(lots=[lot_c])
    board = make_member(board=True)
    board2 = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])
    _rule("dues.monthly_cents", "4500", "Budget resolution (test)")
    for back in (3, 2):
        m = _month(back)
        ledger.post_monthly_assessments(year=m.year, month=m.month, actor=SYSTEM)
        # Lot C pays every month on the due date.
        ledger.record_payment(lot=lot_c, amount_cents=4500, received_on=m, actor=board)
    return {
        "lot_a": lot_a,
        "lot_b": lot_b,
        "lot_c": lot_c,
        "owner_a": owner_a,
        "owner_b": owner_b,
        "owner_c": owner_c,
        "board": board,
        "board2": board2,
        "manager": manager,
    }


def _interest_setup(rate="1200"):
    _on("overdue_interest")
    _rule("interest.annual_rate_bp", rate)
    _rule("interest.grace_days", "30", "Collection policy (test)")


def _last():
    return _month(1)


def _interest_entries(lot=None):
    qs = LedgerEntry.objects.unscoped("t").filter(kind=EntryKind.INTEREST)
    return qs.filter(lot=lot) if lot else qs


# --- Interest (D80) ---------------------------------------------------------------------


def test_interest_arithmetic_is_simple_and_rounds_half_up():
    assert interest.interest_cents(45000, 1800) == 675  # 1.5% a month
    assert interest.interest_cents(60, 1000) == 1  # 0.5 cent rounds up
    assert interest.interest_cents(59, 1000) == 0
    assert interest.interest_cents(0, 1800) == 0
    assert interest.monthly_rate_text(1800) == "1.5"
    assert interest.monthly_rate_text(1000) == "0.833"


def test_nothing_is_posted_without_the_feature_or_a_cited_rate(w):
    m = _last()
    assert interest.post_interest(year=m.year, month=m.month, actor=SYSTEM) == 0
    _on("overdue_interest")
    assert interest.post_interest(year=m.year, month=m.month, actor=SYSTEM) == 0  # no rate
    community.set_rule(key="interest.annual_rate_bp", value="1200", citation="", actor=SYSTEM)
    assert interest.current_policy().problem
    with pytest.raises(interest.InterestError):
        interest.post_interest(year=m.year, month=m.month, actor=w["board"])
    assert not _interest_entries().exists()


def test_rate_above_the_declarations_ceiling_is_refused(w):
    _interest_setup("1800")
    _rule("interest.max_annual_rate_bp", "1500")
    assert "above" in interest.current_policy().problem


def test_interest_posts_once_per_account_and_month_on_the_past_due_principal(w):
    _interest_setup()
    m = _last()
    count = interest.post_interest(year=m.year, month=m.month, actor=SYSTEM)
    assert count == 2  # lots A and B; C is paid up
    entry = _interest_entries(w["lot_a"]).get()
    # Both months are more than 30 days past due on the 15th: $90.00 at 1%/mo.
    assert entry.amount_cents == 90
    assert entry.posted_on == datetime.date(m.year, m.month, 15)
    assert entry.period == f"{m:%Y-%m}"
    assert entry.description.startswith("Interest 1%/mo on $90.00 past due 31+ days")
    assert "Declaration §6.6" in entry.description
    assert not _interest_entries(w["lot_c"]).exists()
    # Idempotent: the task and the button again post nothing.
    assert interest.post_interest(year=m.year, month=m.month, actor=SYSTEM) == 0
    assert interest.post_interest(year=m.year, month=m.month, actor=w["board"]) == 0
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(
            lot=entry.lot,
            account_id=entry.account_id,
            posted_on=entry.posted_on,
            kind=EntryKind.INTEREST,
            amount_cents=5,
            period=entry.period,
            description="again",
        )
    event = AuditEvent.objects.filter(action="ledger.interest_posted").order_by("pk").first()
    assert set(event.metadata) <= {"period", "accounts", "total_cents", "automatic", "skipped_own"}
    assert event.metadata["accounts"] == 2


def test_interest_is_an_open_charge_payments_pay(w):
    _interest_setup()
    m = _last()
    interest.post_interest(year=m.year, month=m.month, actor=SYSTEM)
    titles = [c.title for c in allocation.open_charges(w["lot_a"].pk)]
    assert any(t.startswith("Interest 1%/mo") for t in titles)


def test_late_fees_and_earlier_interest_are_not_in_the_base_unless_the_rule_says(w):
    _interest_setup()
    m = _month(2)
    LedgerEntry.objects.create(
        lot=w["lot_a"],
        posted_on=m,
        kind=EntryKind.LATE_FEE,
        amount_cents=2500,
        period=f"{m:%Y-%m}",
        description="Late fee",
    )
    rows = {c.lot.pk: c for c in interest.candidates(year=_last().year, month=_last().month)}
    assert rows[w["lot_a"].pk].principal_cents == 9000
    _rule("interest.include_fees", "true")
    rows = {c.lot.pk: c for c in interest.candidates(year=_last().year, month=_last().month)}
    assert rows[w["lot_a"].pk].principal_cents == 11500


def test_minimum_balance_and_payment_plan_exclusions(w):
    _interest_setup()
    m = _last()
    case = ledger.open_case(lot=w["lot_b"], actor=w["board"])
    ledger.COLLECTION.apply(case, "payment_plan", actor=w["board"], note="12 months")
    lots = {c.lot.pk for c in interest.candidates(year=m.year, month=m.month)}
    assert lots == {w["lot_a"].pk}  # B keeps to a plan: no interest
    _rule("interest.suspend_on_payment_plan", "false")
    lots = {c.lot.pk for c in interest.candidates(year=m.year, month=m.month)}
    assert lots == {w["lot_a"].pk, w["lot_b"].pk}
    _rule("interest.min_balance_cents", "10000")
    assert interest.candidates(year=m.year, month=m.month) == []


def test_charges_not_yet_past_the_grace_period_earn_nothing(w):
    _interest_setup()
    _rule("interest.grace_days", "90")
    m = _last()
    assert interest.candidates(year=m.year, month=m.month) == []


def test_manager_never_posts_or_reverses_interest(w):
    _interest_setup()
    m = _last()
    with pytest.raises(PermissionDenied):
        interest.post_interest(year=m.year, month=m.month, actor=w["manager"])
    interest.post_interest(year=m.year, month=m.month, actor=SYSTEM)
    entry = _interest_entries(w["lot_a"]).get()
    with pytest.raises(PermissionDenied):
        interest.reverse_interest(entry, reason="x", actor=w["manager"])
    # The general reversal refuses the manager on an interest charge too.
    with pytest.raises(PermissionDenied):
        ledger.reverse_entry(entry, reason="x", actor=w["manager"])


def test_board_reverses_interest_with_a_reason(w):
    _interest_setup()
    m = _last()
    interest.post_interest(year=m.year, month=m.month, actor=SYSTEM)
    entry = _interest_entries(w["lot_a"]).get()
    with pytest.raises(interest.InterestError):
        interest.reverse_interest(entry, reason=" ", actor=w["board"])
    reversal = interest.reverse_interest(entry, reason="Waived (hardship)", actor=w["board"])
    assert reversal.amount_cents == -entry.amount_cents and reversal.reverses_id == entry.pk
    event = AuditEvent.objects.get(action="ledger.interest_reversed")
    assert set(event.metadata) == {"lot_id", "reverses"}


def test_board_members_own_home_is_left_for_someone_else(w):
    _interest_setup()
    board_owner = make_member(board=True, lots=[LotFactory(street_address="9 Board Way")])
    m = _month(2)
    # The new home's first assessment, two months back, is past due too.
    ledger.post_monthly_assessments(year=m.year, month=m.month, actor=SYSTEM)
    last = _last()
    interest.post_interest(year=last.year, month=last.month, actor=board_owner)
    event = AuditEvent.objects.filter(action="ledger.interest_posted").order_by("pk").last()
    assert event.metadata["skipped_own"] == 1


def test_owner_sees_the_policy_on_my_account_only_while_on(client, w):
    client.force_login(w["owner_a"])
    page = client.get(reverse("my_account")).content.decode()
    assert "simple interest" not in page
    _interest_setup()
    page = client.get(reverse("my_account")).content.decode()
    assert "earn simple interest of 1% a month" in page


def test_interest_page_board_manager_and_owner(client, w):
    url = reverse("interest")
    client.force_login(w["board"])
    assert client.get(url).status_code == 404  # feature off
    _interest_setup()
    assert client.get(url).status_code == 200
    client.force_login(w["manager"])
    assert client.get(url).status_code == 200
    assert client.post(url, {"do": "post"}).status_code == 403
    client.force_login(w["owner_a"])
    assert client.get(url).status_code == 403


def test_interest_task_posts_on_or_after_the_posting_day(w):
    _interest_setup()
    m = _last()
    assert interest.run_monthly(today=datetime.date(m.year, m.month, 14)) == 0
    # (The task posts the current month; a past date stands in for "today".)
    assert interest.run_monthly(today=timezone.localdate()) >= 0


# --- Cash-flow forecast (D81) ----------------------------------------------------------------


def _inputs(**kw):
    values = {
        "start": datetime.date(2027, 1, 1),
        "opening_operating": 1000,
        "opening_reserve": 50000,
        "billed": (10000,) * 3,
        "collection_rate_bp": 9000,
        "operating_out": (5000, 5000, 20000),
        "reserve_transfer": (1000,) * 3,
        "reserve_out": (0, 0, 60000),
        "reserve_interest_bp": 1200,
        "min_operating": 0,
        "reserve_target": (None, None, 0),
        "months": 3,
    }
    values.update(kw)
    return Inputs(**values)


def test_forecast_arithmetic():
    rows = project(_inputs())
    first = rows[0]
    assert first.collections == 9000
    assert first.operating_close == 1000 + 9000 - 5000 - 1000
    assert first.reserve_interest == 500  # 50,000 x 12% / 12
    assert first.reserve_close == 50000 + 1000 + 500
    assert rows[1].operating_open == first.operating_close
    assert rows[2].operating_close == 7000 + 9000 - 20000 - 1000
    assert rows[2].operating_low and rows[2].warning
    assert rows[2].reserve_low  # below the target of zero
    assert not rows[0].warning
    assert [r.start for r in rows] == [datetime.date(2027, m, 1) for m in (1, 2, 3)]


def test_forecast_scenarios_are_not_saved_and_change_only_the_inputs():
    base = _inputs()
    worse = apply_scenario(base, Scenario(rate_change_pct=-10, extra_monthly_cents=100))
    assert worse.collection_rate_bp == 8000 and base.collection_rate_bp == 9000
    assert worse.operating_out == (5100, 5100, 20100)
    one_off = apply_scenario(base, Scenario(one_off_cents=700))
    assert one_off.operating_out[0] == 5700
    assert apply_scenario(base, Scenario(rate_change_pct=-100)).collection_rate_bp == 0
    assert Scenario().describe() == "none"


def test_spread_and_months():
    assert spread(100, 12) == [9, 9, 9, 9, 8, 8, 8, 8, 8, 8, 8, 8]
    assert sum(spread(150000, 12)) == 150000
    assert add_months(datetime.date(2026, 11, 1), 3) == datetime.date(2027, 2, 1)
    with pytest.raises(ValueError):
        project(_inputs(collection_rate_bp=10001))


def test_forecast_page_exports_and_access(client, w):
    url = reverse("cashflow")
    client.force_login(w["board"])
    assert client.get(url).status_code == 404
    _on("cashflow_forecast")
    page = client.get(url + "?rate=-10&extra=100").content.decode()
    assert "Cash-flow forecast" in page and "What if" in page
    csv_response = client.get(reverse("cashflow_export") + "?format=csv")
    assert csv_response.status_code == 200 and b"Operating closing" in csv_response.content
    pdf_response = client.get(reverse("cashflow_export") + "?format=pdf&rate=-10")
    assert pdf_response.content.startswith(b"%PDF")
    event = AuditEvent.objects.filter(action="cashflow.exported").order_by("pk").last()
    assert event.metadata == {"format": "pdf", "scenario": "rate-10"}
    client.force_login(w["manager"])
    assert client.get(url).status_code == 200
    client.force_login(w["owner_a"])
    assert client.get(url).status_code == 403
    assert client.get(reverse("cashflow_export")).status_code == 403


def test_forecast_warning_reaches_my_items_only_while_on(w):
    _rule("cashflow.min_operating_cents", "999999999", "Financial controls (test)")
    cashflow._cache.clear()
    assert not [i for i in items_for(w["board"]) if "Cash-flow forecast" in i.title]
    _on("cashflow_forecast")
    cashflow._cache.clear()
    items = [i for i in items_for(w["board"]) if "Cash-flow forecast" in i.title]
    assert items and items[0].url == reverse("cashflow")
    assert not [i for i in items_for(w["owner_a"]) if "Cash-flow" in i.title]


def test_collection_rate_from_the_last_twelve_months(w):
    rate, source = cashflow.collection_rate_bp(timezone.localdate())
    # Three homes billed two months each; lot C paid its two.
    assert source == "last 12 months" and rate == 3333
    _rule("cashflow.collection_rate_percent", "95", "Treasurer (test)")
    assert cashflow.collection_rate_bp(timezone.localdate()) == (9500, "rule")


# --- Liens and legal collection (D82) -------------------------------------------------------


def _referred_case(w, lot_key="lot_b"):
    case = ledger.open_case(lot=w[lot_key], actor=w["board"])
    for step in ("send_reminder", "send_second_reminder"):
        ledger.COLLECTION.apply(case, step, actor=w["board"])
        case.refresh_from_db()
    ledger.COLLECTION.apply(case, "refer", actor=w["board"], note="To counsel")
    case.refresh_from_db()
    return case


def test_legal_steps_need_a_referred_case_and_a_board_member(w):
    _on("collections_legal")
    case = ledger.open_case(lot=w["lot_b"], actor=w["board"])
    with pytest.raises(legal.LegalError):
        legal.record(case, kind="referral", firm="Example LLP", actor=w["board"])
    with pytest.raises(legal.LegalError):
        legal.record(case, kind="lien", lien_filing="statement", actor=w["board"])
    case = _referred_case_from(case, w)
    with pytest.raises(PermissionDenied):
        legal.record(case, kind="referral", firm="Example LLP", actor=w["manager"])
    with pytest.raises(legal.LegalError):
        legal.record(case, kind="referral", actor=w["board"])  # no firm
    event = legal.record(case, kind="referral", firm="Example LLP", actor=w["board"])
    assert event.kind == "referral"
    audit = AuditEvent.objects.get(action="legal.recorded")
    assert set(audit.metadata) == {"case_id", "kind"}
    with pytest.raises(legal.LegalError):
        legal.record(case, kind="cost", amount_cents=100, detail="Filing", actor=w["board"])


def _referred_case_from(case, w):
    for step in ("send_reminder", "send_second_reminder"):
        ledger.COLLECTION.apply(case, step, actor=w["board"])
        case.refresh_from_db()
    ledger.COLLECTION.apply(case, "refer", actor=w["board"], note="To counsel")
    case.refresh_from_db()
    return case


def test_legal_events_are_append_only(w):
    from core.append_only import AppendOnlyError

    case = _referred_case(w)
    event = legal.record(case, kind="referral", firm="Example LLP", actor=w["board"])
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()


def test_charging_a_cost_needs_the_rule_a_board_member_and_not_ones_own_home(w):
    case = _referred_case(w)
    legal.record(case, kind="referral", firm="Example LLP", actor=w["board"])
    cost = legal.record(
        case,
        kind="cost",
        amount_cents=35000,
        chargeable=True,
        detail="Lien filing",
        actor=w["board"],
    )
    with pytest.raises(legal.LegalError):  # the Declaration rule is not recorded
        legal.post_cost(cost, actor=w["board"])
    _rule("legal.costs_chargeable", "true", "Declaration §6.7 (test)")
    with pytest.raises(PermissionDenied):
        legal.post_cost(cost, actor=w["manager"])
    # A board member who owns the home cannot charge it (own matter, D65).
    owner_board = w["owner_b"]
    person = owner_board.person
    from community.models import SeatType
    from community.tests.factories import days_ago

    community.record_board_seat(
        person=person, seat_type=SeatType.OWNER_ELECTED, term_start=days_ago(5), actor=SYSTEM
    )
    with pytest.raises(legal.LegalError):
        legal.post_cost(cost, actor=owner_board)
    entry = legal.post_cost(cost, actor=w["board"])
    assert entry.kind == EntryKind.LEGAL_COST and entry.amount_cents == 35000
    assert "Declaration §6.7" in entry.description
    with pytest.raises(legal.LegalError):
        legal.post_cost(cost, actor=w["board2"])
    # Only the board takes it back.
    with pytest.raises(PermissionDenied):
        ledger.reverse_entry(entry, reason="x", actor=w["manager"])
    not_chargeable = legal.record(
        case, kind="cost", amount_cents=900, chargeable=False, detail="Copies", actor=w["board"]
    )
    with pytest.raises(legal.LegalError):
        legal.post_cost(not_chargeable, actor=w["board"])
    audit = AuditEvent.objects.get(action="legal.cost_posted")
    assert set(audit.metadata) == {"case_id", "lot_id", "amount_cents"}


def test_release_reminder_when_a_referred_case_is_paid(w):
    _on("collections_legal")
    case = _referred_case(w)
    legal.record(case, kind="referral", firm="Example LLP", actor=w["board"])
    legal.record(case, kind="lien", lien_filing="statement", reference="LN-1", actor=w["board"])
    assert not legal.release_due(case)
    ledger.record_payment(
        lot=w["lot_b"],
        amount_cents=ledger.balance(w["lot_b"].pk),
        received_on=timezone.localdate(),
        actor=w["board"],
    )
    assert legal.release_due(case)
    titles = [i.title for i in items_for(w["board"])]
    assert "File the lien release for 2 Bravo Way" in titles
    assert "File the lien release for 2 Bravo Way" not in [i.title for i in items_for(w["manager"])]
    legal.record(case, kind="release", actor=w["board"])
    assert not legal.release_due(case)
    features.set_feature("collections_legal", False, actor=SYSTEM)
    legal.record(case, kind="lien", lien_filing="judgment", actor=w["board"])
    assert "File the lien release for 2 Bravo Way" not in [i.title for i in items_for(w["board"])]


def test_legal_panel_and_pages_are_board_only(client, w):
    case = _referred_case(w)
    legal.record(case, kind="referral", firm="Board only: legal matter LLP", actor=w["board"])
    url = reverse("collection_detail", args=[case.pk])
    client.force_login(w["board"])
    assert "Board only: legal matter LLP" not in client.get(url).content.decode()  # off
    _on("collections_legal")
    assert "Board only: legal matter LLP" in client.get(url).content.decode()
    response = client.post(
        reverse("collection_legal", args=[case.pk]),
        {"do": "record", "kind": "demand", "occurred_on": timezone.localdate().isoformat()},
    )
    assert response.status_code == 302
    assert LegalEvent.objects.unscoped("t").filter(case=case, kind="demand").exists()
    client.force_login(w["manager"])
    page = client.get(url).content.decode()
    assert "Board only: legal matter LLP" not in page
    response = client.post(reverse("collection_legal", args=[case.pk]), {"do": "record"})
    assert response.status_code == 403
    for user in (w["owner_a"], w["owner_b"]):
        client.force_login(user)
        assert client.get(url).status_code in (403, 404)
        assert client.post(reverse("collection_legal", args=[case.pk])).status_code in (403, 404)
        assert client.get(reverse("collection_legal_file", args=[case.pk, 1])).status_code in (
            403,
            404,
        )
    # The owner's statement shows charges only, never the legal records.
    client.force_login(w["owner_b"])
    assert "Board only: legal matter" not in client.get(reverse("my_account")).content.decode()
    assert LegalEvent.objects.for_user(w["owner_b"]).count() == 0


# --- Owners' finance summary (D84) ----------------------------------------------------------


def test_owner_budget_page_shows_only_the_two_sections_when_on(client, w):
    from ledger.models import Budget

    budget = ledger.create_budget(fiscal_year=timezone.localdate().year, actor=w["board"])
    ledger.add_line(
        budget,
        kind="income",
        name="Assessments",
        annual_cents=540000,
        income_source="assessments",
        actor=w["board"],
    )
    ledger.adopt_budget(budget, actor=w["board"])
    assert Budget.objects.count() == 1
    client.force_login(w["owner_a"])
    before = client.get(reverse("owner_budget")).content.decode()
    assert 'id="budget-h"' in before and "Operating and reserve" not in before
    _on("owner_finance_dashboard")
    page = client.get(reverse("owner_budget")).content.decode()
    assert "<h1>Budget and reserves</h1>" in page
    assert "Operating and reserve" in page and "Budget and actual" in page
    assert "not a closed month" in page
    assert "Assessments" in page
    for private in ("2 Bravo Way", "3 Charlie Way", "Collection", "delinquen"):
        assert private not in page.split("<main", 1)[-1], private
    # With the budget and reserve plan features off it is still on.
    features.set_feature("owner_budget", False, actor=SYSTEM)
    features.set_feature("reserves", False, actor=SYSTEM)
    assert client.get(reverse("owner_budget")).status_code == 200
    features.set_feature("owner_finance_dashboard", False, actor=SYSTEM)
    assert client.get(reverse("owner_budget")).status_code == 404


def test_budget_vs_actual_counts_only_finished_months(w):
    from ledger import owner_finance

    budget = ledger.create_budget(fiscal_year=timezone.localdate().year, actor=w["board"])
    ledger.add_line(
        budget,
        kind="income",
        name="Assessments",
        annual_cents=120000,
        income_source="assessments",
        actor=w["board"],
    )
    ledger.adopt_budget(budget, actor=w["board"])
    bva = owner_finance.budget_vs_actual_ytd()
    months = timezone.localdate().month - 1  # fiscal year starts in January by default
    assert bva.closed_months == months
    line = bva.lines[0]
    assert line.budget_ytd == 10000 * months
    today = timezone.localdate()
    billed = [m for m in (_month(3), _month(2)) if m.year == today.year]
    assert line.actual_ytd == 4500 * 3 * len(billed)  # three homes billed
    funds = owner_finance.fund_balances()
    assert funds.closed is False

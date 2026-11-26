"""Reserve plan: board changes, adoption snapshots (append-only), owner
summary, budget and letters hints, CSV export, permissions and the feature
switch."""

import csv
import datetime
import io

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, InternalError, ProgrammingError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import CommunityProfile, Role
from community.tests.factories import LotFactory, make_member
from core import features
from core.append_only import AppendOnlyError
from core.db_roles import append_only_tables
from core.policy import SYSTEM
from ledger import reserve_plan
from ledger import services as ledger
from ledger.models import ReserveComponent, ReservePlanSettings, ReserveStudySnapshot

pytestmark = pytest.mark.django_db


def _component(actor, **kw):
    values = {
        "name": "Main road paving",
        "category": "roads",
        "quantity": "1,200 sq yd",
        "replacement_cost_cents": 18000000,
        "useful_life_years": 25,
        "remaining_life_years": 6,
        "last_replaced_year": None,
        "notes": "Board note: get three paving bids",
        "active": True,
    }
    values.update(kw)
    return reserve_plan.save_component(actor=actor, **values)


def _settings(actor, **kw):
    values = {
        "starting_balance_cents": 2500000,
        "balance_as_of": datetime.date(timezone.localdate().year, 1, 1),
        "annual_contribution_cents": 600000,
        "inflation_bp": 300,
        "interest_bp": 100,
        "horizon_years": 30,
    }
    values.update(kw)
    return reserve_plan.save_settings(actor=actor, **values)


@pytest.fixture
def w():
    lot = LotFactory()
    board = make_member(board=True)
    return {
        "lot": lot,
        "board": board,
        "owner": make_member(lots=[lot]),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "tenant": None,
    }


# --- Services ---------------------------------------------------------------------------------


def test_board_saves_components_with_history_and_audit(w):
    c = _component(w["board"])
    assert c.history.count() == 1
    reserve_plan.save_component(
        c,
        actor=w["board"],
        name="Main road paving",
        category="roads",
        replacement_cost_cents=19000000,
        useful_life_years=25,
        remaining_life_years=5,
        active=True,
    )
    c.refresh_from_db()
    assert c.replacement_cost_cents == 19000000 and c.history.count() == 2
    assert list(c.history.values_list("replacement_cost_cents", flat=True)) == [
        19000000,
        18000000,
    ]
    events = AuditEvent.objects.filter(action="reserves.component_saved")
    assert events.count() == 2
    assert events.first().metadata.keys() <= {"created", "category", "cost_cents", "active"}


def test_component_needs_years_left_or_last_replaced(w):
    with pytest.raises(reserve_plan.ReserveError):
        _component(w["board"], remaining_life_years=None, last_replaced_year=None)
    with pytest.raises(reserve_plan.ReserveError):
        _component(w["board"], useful_life_years=0)
    with pytest.raises(reserve_plan.ReserveError):
        _component(w["board"], last_replaced_year=timezone.localdate().year + 1)
    with pytest.raises(reserve_plan.ReserveError):
        _component(w["board"], name="  ")


def test_last_replaced_year_gives_years_left(w):
    year = timezone.localdate().year
    c = _component(w["board"], remaining_life_years=None, last_replaced_year=year - 20)
    assert c.remaining_life(year) == 5
    assert c.remaining_life(year + 10) == 0  # overdue: due in the first year


def test_components_are_never_deleted(w):
    c = _component(w["board"])
    with pytest.raises(PermissionError):
        c.delete()


def test_settings_validation_and_audit(w):
    with pytest.raises(reserve_plan.ReserveError):
        _settings(w["board"], inflation_bp=2500)
    with pytest.raises(reserve_plan.ReserveError):
        _settings(w["board"], horizon_years=2)
    with pytest.raises(reserve_plan.ReserveError):
        _settings(w["board"], annual_contribution_cents=-1)
    assert not ReservePlanSettings.objects.filter(annual_contribution_cents=-1).exists()
    plan = _settings(w["board"])
    assert plan.history.first().annual_contribution_cents == 600000
    assert plan.history.first().history_type == "~"
    assert AuditEvent.objects.filter(action="reserves.settings_saved").count() == 1


def test_starting_balance_falls_back_to_resale_figure(w):
    assert reserve_plan.starting_balance().source == "none"
    profile = CommunityProfile.get()
    profile.resale_reserves_cents = 4200000
    profile.save()
    balance = reserve_plan.starting_balance()
    assert (balance.cents, balance.source) == (4200000, "letters")
    _settings(w["board"], starting_balance_cents=100)
    assert reserve_plan.starting_balance().source == "typed"


def test_inactive_components_are_left_out(w):
    _component(w["board"])
    _component(w["board"], name="Old fence", category="fencing", active=False)
    inputs, _ = reserve_plan.current_inputs()
    assert [c.name for c in inputs.components] == ["Main road paving"]


def test_projection_uses_settings_and_components(w):
    _settings(w["board"], inflation_bp=0, interest_bp=0, horizon_years=10)
    _component(w["board"])
    result, _ = reserve_plan.current_projection()
    year = timezone.localdate().year
    assert result.rows[0].year == year and len(result.rows) == 10
    # 25,000 + 6 x 6,000 + 6,000 (year 6) = 67,000 < 180,000: below zero in year 6.
    assert result.rows[6].expense_cents == 18000000
    assert result.first_negative_year == year + 6
    # Age 19 of 25: 180,000 x 19 / 25 = 136,800; 25,000 / 136,800 = 18%.
    assert result.fully_funded_cents == 13680000 and result.percent_funded == 18


# --- Adoption snapshot --------------------------------------------------------------------------


def test_adopt_records_snapshot_and_audit(w):
    _settings(w["board"])
    _component(w["board"])
    snap = reserve_plan.adopt(actor=w["board"])
    result, _ = reserve_plan.current_projection()
    assert snap.adopted_on == timezone.localdate() and snap.adopted_by == w["board"]
    assert snap.needed_contribution_cents == result.needed_contribution_cents
    assert snap.percent_funded == result.percent_funded
    assert snap.components[0]["name"] == "Main road paving"
    assert "notes" not in snap.components[0]  # board notes never reach the snapshot
    assert snap.next_items[0]["name"] == "Main road paving"
    event = AuditEvent.objects.get(action="reserves.adopted")
    assert event.metadata["components"] == 1
    # A later adoption is a new record; the latest is the one cited.
    snap2 = reserve_plan.adopt(actor=w["board"])
    assert reserve_plan.latest_snapshot() == snap2
    assert ReserveStudySnapshot.objects.count() == 2


def test_adopt_needs_a_component_and_a_person(w):
    with pytest.raises(reserve_plan.ReserveError):
        reserve_plan.adopt(actor=w["board"])
    _component(w["board"])
    with pytest.raises(reserve_plan.ReserveError):
        reserve_plan.adopt(actor=SYSTEM)


def test_snapshot_is_append_only_in_the_model(w):
    _component(w["board"])
    snap = reserve_plan.adopt(actor=w["board"])
    snap.percent_funded = 100
    with pytest.raises(AppendOnlyError):
        snap.save()
    with pytest.raises(AppendOnlyError):
        snap.delete()
    with pytest.raises(AppendOnlyError):
        ReserveStudySnapshot.objects.filter(pk=snap.pk).update(percent_funded=100)
    with pytest.raises(AppendOnlyError):
        ReserveStudySnapshot.objects.all().delete()


def test_snapshot_is_append_only_in_the_database(w):
    _component(w["board"])
    snap = reserve_plan.adopt(actor=w["board"])
    refused = (InternalError, IntegrityError, ProgrammingError)
    with pytest.raises(refused), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE ledger_reservestudysnapshot SET percent_funded = 100 WHERE id = %s",
            [snap.pk],
        )
    with pytest.raises(refused), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM ledger_reservestudysnapshot WHERE id = %s", [snap.pk])
    snap.refresh_from_db()
    assert snap.percent_funded != 100


def test_runtime_role_cannot_change_snapshots(settings):
    table = ReserveStudySnapshot._meta.db_table
    assert table in append_only_tables()
    with connection.cursor() as cursor:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            cursor.execute(
                "SELECT has_table_privilege(%s, %s, %s)", [settings.DB_APP_ROLE, table, privilege]
            )
            assert cursor.fetchone()[0] is False, privilege
        cursor.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT')", [settings.DB_APP_ROLE, table]
        )
        assert cursor.fetchone()[0] is True


# --- Permissions --------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["manager", "auditor", "owner"])
def test_only_the_board_changes_or_adopts_the_plan(w, who):
    _component(w["board"])
    with pytest.raises(PermissionDenied):
        _component(w[who])
    with pytest.raises(PermissionDenied):
        _settings(w[who])
    with pytest.raises(PermissionDenied):
        reserve_plan.adopt(actor=w[who])


def test_owner_cannot_export(w):
    with pytest.raises(PermissionDenied):
        reserve_plan.export_csv(actor=w["owner"])


@pytest.mark.parametrize("who", ["board", "manager", "auditor"])
def test_board_manager_and_auditor_view_the_plan(client, w, who):
    _component(w["board"])
    client.force_login(w[who])
    page = client.get(reverse("reserve_plan"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "<h1>Reserve plan</h1>" in html and "Main road paving" in html
    assert ("Record as adopted" in html) == (who == "board")
    assert ("Add a component" in html) == (who == "board")
    detail = client.get(reverse("reserve_component", args=[ReserveComponent.objects.get().pk]))
    assert detail.status_code == 200
    assert ("Change this component" in detail.content.decode()) == (who == "board")


def test_manager_posts_are_refused(client, w):
    c = _component(w["board"])
    client.force_login(w["manager"])
    assert client.post(reverse("reserve_adopt")).status_code == 403
    assert (
        client.post(
            reverse("reserve_plan"),
            {"section": "settings", "annual_contribution_cents": "1", "inflation_bp": "1"},
        ).status_code
        == 403
    )
    assert client.post(reverse("reserve_component", args=[c.pk]), {"name": "X"}).status_code == 403
    assert not ReserveStudySnapshot.objects.exists()


def test_owner_sees_summary_only(client, w):
    _component(w["board"])
    client.force_login(w["owner"])
    for name, args in (
        ("reserve_plan", []),
        ("reserve_export", []),
        ("reserve_component", [ReserveComponent.objects.get().pk]),
    ):
        assert client.get(reverse(name, args=args)).status_code == 403, name
    assert client.post(reverse("reserve_adopt")).status_code == 403
    page = client.get(reverse("owner_budget")).content.decode()
    assert "No reserve plan adopted yet" in page and "Main road paving" not in page
    reserve_plan.adopt(actor=w["board"])
    page = client.get(reverse("owner_budget")).content.decode()
    assert "reserve plan adopted" in page and "Main road paving" in page
    assert "three paving bids" not in page  # no board notes


def test_owner_summary_shows_only_adopted_figures(client, w):
    _settings(w["board"], starting_balance_cents=2500000)
    _component(w["board"])
    reserve_plan.adopt(actor=w["board"])
    _settings(w["board"], starting_balance_cents=9900000)  # draft change after adoption
    client.force_login(w["owner"])
    page = client.get(reverse("owner_budget")).content.decode()
    assert "$25,000.00" in page and "$99,000.00" not in page


def test_summary_is_on_budget_and_reserves(client, w):
    """Streamlining: the summary is a section of Budget and reserves; the old
    page redirects there."""
    client.force_login(w["owner"])
    response = client.get(reverse("reserve_summary"))
    assert response.status_code == 302 and response["Location"] == reverse("owner_budget")
    page = client.get(reverse("owner_budget")).content.decode()
    assert "<h1>Budget and reserves</h1>" in page and 'id="reserves"' in page


def test_budget_and_reserves_shows_only_the_parts_switched_on(client, w):
    client.force_login(w["owner"])
    features.set_feature("reserves", False, actor=w["board"])
    page = client.get(reverse("owner_budget")).content.decode()
    assert 'id="budget"' in page and 'id="reserves"' not in page
    features.set_feature("owner_budget", False, actor=w["board"])
    assert client.get(reverse("owner_budget")).status_code == 404  # both off
    features.set_feature("reserves", True, actor=w["board"])
    page = client.get(reverse("owner_budget")).content.decode()
    assert 'id="budget"' not in page and 'id="reserves"' in page


# --- Board page forms ---------------------------------------------------------------------------


def test_board_adds_component_typed_in_dollars(client, w):
    client.force_login(w["board"])
    response = client.post(
        reverse("reserve_plan"),
        {
            "section": "component",
            "name": "Entrance sign",
            "category": "signs",
            "quantity": "2 signs",
            "replacement_cost_cents": "8,000.50",
            "useful_life_years": "15",
            "remaining_life_years": "0",
            "active": "on",
        },
    )
    assert response.status_code == 302
    c = ReserveComponent.objects.get()
    assert c.replacement_cost_cents == 800050 and c.remaining_life_years == 0
    plan = client.get(reverse("reserve_plan")).content.decode()
    assert "Entrance sign $8,000.50" in plan  # due in the first year


def test_invalid_component_rerenders_bound_with_errors(client, w):
    client.force_login(w["board"])
    response = client.post(
        reverse("reserve_plan"),
        {
            "section": "component",
            "name": "Pond",
            "category": "stormwater",
            "replacement_cost_cents": "abc",
            "useful_life_years": "20",
        },
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert 'role="alert"' in html and 'value="Pond"' in html
    assert "Enter the years left, or the year it was last replaced." in html
    assert not ReserveComponent.objects.exists()


def test_settings_form_takes_percents_and_dollars(client, w):
    client.force_login(w["board"])
    response = client.post(
        reverse("reserve_plan"),
        {
            "section": "settings",
            "starting_balance_cents": "25,000",
            "balance_as_of": f"{timezone.localdate().year}-01-01",
            "annual_contribution_cents": "6000",
            "inflation_bp": "3.25%",
            "interest_bp": "0.5",
            "horizon_years": "30",
        },
    )
    assert response.status_code == 302
    plan = ReservePlanSettings.get()
    assert (plan.starting_balance_cents, plan.annual_contribution_cents) == (2500000, 600000)
    assert (plan.inflation_bp, plan.interest_bp) == (325, 50)
    page = client.get(reverse("reserve_plan")).content.decode()
    assert 'value="3.25"' in page and 'value="0.5"' in page


def test_settings_service_refusal_shows_on_form(client, w):
    client.force_login(w["board"])
    response = client.post(
        reverse("reserve_plan"),
        {
            "section": "settings",
            "annual_contribution_cents": "6000",
            "inflation_bp": "30",
            "interest_bp": "1",
            "horizon_years": "30",
        },
    )
    assert response.status_code == 200
    assert "Inflation must be between 0% and 20%." in response.content.decode()


def test_edit_component_and_history_shown(client, w):
    c = _component(w["board"])
    client.force_login(w["board"])
    response = client.post(
        reverse("reserve_component", args=[c.pk]),
        {
            "name": "Main road paving",
            "category": "roads",
            "replacement_cost_cents": "190000",
            "useful_life_years": "25",
            "remaining_life_years": "6",
        },
    )
    assert response.status_code == 302
    c.refresh_from_db()
    assert c.replacement_cost_cents == 19000000 and c.active is False  # box left clear
    page = client.get(reverse("reserve_component", args=[c.pk])).content.decode()
    assert "$190,000.00" in page and "$180,000.00" in page


def test_negative_years_are_highlighted(client, w):
    _settings(w["board"], starting_balance_cents=0, annual_contribution_cents=0)
    _component(w["board"], remaining_life_years=0)
    client.force_login(w["board"])
    html = client.get(reverse("reserve_plan")).content.decode()
    assert 'class="below-zero"' in html and "Below zero" in html
    assert "drops below zero in" in html


def test_board_adopts_from_page(client, w):
    _component(w["board"])
    client.force_login(w["board"])
    assert client.post(reverse("reserve_adopt")).status_code == 302
    assert ReserveStudySnapshot.objects.count() == 1
    assert client.get(reverse("reserve_adopt")).status_code == 405


# --- Export --------------------------------------------------------------------------------------


def test_csv_export_is_audited_and_safe(client, w):
    _settings(w["board"], horizon_years=10)
    _component(w["board"], name="=HYPERLINK(1)", remaining_life_years=0)
    client.force_login(w["manager"])
    response = client.get(reverse("reserve_export"))
    assert response.status_code == 200 and response["Content-Type"] == "text/csv"
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0][:2] == ["Year", "Starting balance"] and len(rows) == 11
    assert rows[1][7].startswith("'=")  # no spreadsheet formulas
    assert rows[1][1] == "25000.00"
    event = AuditEvent.objects.get(action="reserves.exported")
    assert event.metadata == {"rows": 10, "start_year": timezone.localdate().year}


# --- Budget and resale certificate hints -----------------------------------------------------


def test_budget_page_shows_adopted_suggestion(client, w):
    budget = ledger.create_budget(fiscal_year=timezone.localdate().year + 1, actor=w["board"])
    client.force_login(w["board"])
    assert (
        "Suggested reserve contribution"
        not in client.get(reverse("budget_detail", args=[budget.pk])).content.decode()
    )
    _component(w["board"])
    snap = reserve_plan.adopt(actor=w["board"])
    page = client.get(reverse("budget_detail", args=[budget.pk])).content.decode()
    assert "Suggested reserve contribution" in page
    assert f"reserve plan adopted {snap.adopted_on:%B} {snap.adopted_on.day}" in page
    features.set_feature("reserves", False, actor=w["board"])
    page = client.get(reverse("budget_detail", args=[budget.pk])).content.decode()
    assert "Suggested reserve contribution" not in page


def test_letters_page_offers_adopted_balance_without_changing_certificate(client, w):
    _settings(w["board"], starting_balance_cents=3141500)
    _component(w["board"])
    reserve_plan.adopt(actor=w["board"])
    client.force_login(w["board"])
    page = client.get(reverse("letters_settings")).content.decode()
    assert "data-reserve-hint" in page and "$31,415.00" in page
    assert CommunityProfile.get().resale_reserves_cents is None  # nothing copied silently


# --- Feature switch -------------------------------------------------------------------------------


def test_feature_switch_hides_every_page(client, w):
    c = _component(w["board"])
    features.set_feature("reserves", False, actor=w["board"])
    client.force_login(w["board"])
    for name, args in (
        ("reserve_plan", []),
        ("reserve_export", []),
        ("reserve_component", [c.pk]),
        ("reserve_summary", []),
    ):
        assert client.get(reverse(name, args=args)).status_code == 404, name
    assert client.post(reverse("reserve_adopt")).status_code == 404
    html = client.get(reverse("budgets")).content.decode()
    assert reverse("reserve_plan") not in html


def test_reserve_plan_is_in_the_finance_menu_and_tabs(client, w):
    client.force_login(w["manager"])
    html = client.get(reverse("budgets")).content.decode()
    assert html.count(f'href="{reverse("reserve_plan")}"') >= 2  # sidebar and Finance tab

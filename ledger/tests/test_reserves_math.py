"""Reserve plan arithmetic (ledger/reserves.py), checked by hand.

Every expected figure below was worked out on paper from the rules in the
module docstring; the comments show the working.
"""

import itertools
from fractions import Fraction

import pytest

from ledger.reserves import (
    ComponentInput,
    PlanInputs,
    analyse,
    fully_funded_cents,
    inflated_cost,
    interest_on,
    needed_contribution,
    per_lot_monthly,
    percent_funded,
    project,
    round_half_up,
)


def plan(components=(), **kw):
    values = {
        "start_year": 2027,
        "starting_balance_cents": 0,
        "annual_contribution_cents": 0,
        "inflation_bp": 0,
        "interest_bp": 0,
        "horizon_years": 5,
    }
    values.update(kw)
    return PlanInputs(components=tuple(components), **values)


def test_round_half_up():
    assert round_half_up(Fraction(5, 2)) == 3
    assert round_half_up(Fraction(49, 10)) == 5
    assert round_half_up(Fraction(41, 10)) == 4
    assert round_half_up(Fraction(-5, 2)) == -3
    assert round_half_up(Fraction(0)) == 0


def test_component_due_in_year_zero_with_no_money_goes_negative():
    # $1,000 in hand, $500 a year, a $2,000 roof due now and every 3 years.
    roof = ComponentInput("Roof", 200000, useful_life=3, remaining_life=0)
    rows = project(plan([roof], starting_balance_cents=100000, annual_contribution_cents=50000))
    assert [r.year for r in rows] == [2027, 2028, 2029, 2030, 2031]
    # y0: 1000 + 500 - 2000 = -500; y1: 0; y2: 500; y3: 500 + 500 - 2000 = -1000; y4: -500
    assert [r.end_cents for r in rows] == [-50000, 0, 50000, -100000, -50000]
    assert [r.expense_cents for r in rows] == [200000, 0, 0, 200000, 0]
    assert [r.negative for r in rows] == [True, False, False, True, True]
    assert rows[0].items == (("Roof", 200000),)
    assert rows[1].begin_cents == -50000


def test_needed_contribution_for_year_zero_expense():
    roof = ComponentInput("Roof", 200000, useful_life=3, remaining_life=0)
    inputs = plan([roof], starting_balance_cents=100000, annual_contribution_cents=50000)
    # Year 0 needs 1000 + C >= 2000 (C >= 1000); year 3 needs 1000 + 4C >= 4000
    # (C >= 750). So $1,000.00.
    assert needed_contribution(inputs) == 100000
    result = analyse(inputs)
    assert result.needed_contribution_cents == 100000
    assert result.first_negative_year == 2027
    assert result.negative_years == [2027, 2030, 2031]
    assert result.lowest_end_cents == -100000 and result.lowest_year == 2030
    assert result.total_expense_cents == 400000
    # Age 3 of 3 years: fully funded is the whole $2,000; $1,000 is 50%.
    assert result.fully_funded_cents == 200000
    assert result.percent_funded == 50
    assert result.next_items == ((2027, "Roof", 200000), (2030, "Roof", 200000))


def test_replacements_repeat_every_useful_life():
    sealcoat = ComponentInput("Sealcoat", 1000, useful_life=2, remaining_life=1)
    mulch = ComponentInput("Mulch", 300, useful_life=1, remaining_life=0)
    rows = project(plan([sealcoat, mulch], horizon_years=6))
    # Sealcoat in years 1, 3, 5; mulch every year.
    assert [r.expense_cents for r in rows] == [300, 1300, 300, 1300, 300, 1300]
    assert [name for name, _ in rows[3].items] == ["Sealcoat", "Mulch"]


def test_component_beyond_horizon_never_appears():
    pond = ComponentInput("Pond dredge", 5000000, useful_life=25, remaining_life=8)
    rows = project(plan([pond], horizon_years=8))
    assert all(r.expense_cents == 0 for r in rows)
    assert project(plan([pond], horizon_years=9))[8].expense_cents == 5000000


def test_inflation_compounds_from_year_zero():
    # $10,000 at 3%: year 2 = 10,000 x 1.0609 = $10,609.00;
    # year 12 = 10,000 x 1.03^12 = 10,000 x 1.425760886846... = $14,257.61.
    assert inflated_cost(1000000, 300, 0) == 1000000
    assert inflated_cost(1000000, 300, 2) == 1060900
    assert inflated_cost(1000000, 300, 12) == 1425761
    paving = ComponentInput("Paving", 1000000, useful_life=10, remaining_life=2)
    rows = project(plan([paving], inflation_bp=300, horizon_years=15))
    assert rows[2].expense_cents == 1060900
    assert rows[12].expense_cents == 1425761
    assert sum(r.expense_cents for r in rows) == 1060900 + 1425761


def test_inflated_cost_rounds_half_up_once_per_item():
    # 50 cents at 1% = 50.5 cents -> 51.
    assert inflated_cost(50, 100, 1) == 51
    # 49 cents at 1% = 49.49 -> 49.
    assert inflated_cost(49, 100, 1) == 49


def test_interest_on_start_of_year_balance():
    rows = project(plan(starting_balance_cents=1000000, interest_bp=200, horizon_years=3))
    # 2% of 10,000.00 = 200.00; of 10,200.00 = 204.00; of 10,404.00 = 208.08.
    assert [r.interest_cents for r in rows] == [20000, 20400, 20808]
    assert [r.end_cents for r in rows] == [1020000, 1040400, 1061208]


def test_interest_rounding_and_negative_balances():
    assert interest_on(150, 100) == 2  # 1.5 cents -> 2
    assert interest_on(149, 100) == 1  # 1.49 -> 1
    assert interest_on(-50000, 500) == 0  # a deficit earns nothing
    assert interest_on(0, 500) == 0
    assert interest_on(123456, 0) == 0


def test_interest_and_expense_in_the_same_year():
    # Start 1,000.00, 5% interest, 100.00 a year, a 500.00 item in year 1.
    fence = ComponentInput("Fence", 50000, useful_life=10, remaining_life=1)
    rows = project(
        plan(
            [fence],
            starting_balance_cents=100000,
            annual_contribution_cents=10000,
            interest_bp=500,
            horizon_years=3,
        )
    )
    # y0: 1000 + 100 + 50 = 1150; y1: 1150 + 100 + 57.50 - 500 = 807.50;
    # y2: 807.50 + 100 + 40.375 (-> 40.38) = 947.88
    assert [r.end_cents for r in rows] == [115000, 80750, 94788]
    assert [r.interest_cents for r in rows] == [5000, 5750, 4038]


def test_fully_funded_sums_exactly_then_rounds_once():
    # Each: 100 cents x age 2 / life 3 = 66.67; two of them = 133.33 -> 133.
    a = ComponentInput("A", 100, useful_life=3, remaining_life=1)
    b = ComponentInput("B", 100, useful_life=3, remaining_life=1)
    assert fully_funded_cents(plan([a])) == 67
    assert fully_funded_cents(plan([a, b])) == 133


def test_fully_funded_ages():
    new = ComponentInput("New sign", 400000, useful_life=20, remaining_life=20)
    longer = ComponentInput("Longer than life", 400000, useful_life=20, remaining_life=25)
    half = ComponentInput("Half way", 400000, useful_life=20, remaining_life=10)
    worn = ComponentInput("Worn out", 400000, useful_life=20, remaining_life=0)
    assert new.age == 0 and longer.age == 0 and half.age == 10 and worn.age == 20
    assert fully_funded_cents(plan([new, longer])) == 0
    assert fully_funded_cents(plan([half, worn])) == 200000 + 400000


def test_percent_funded():
    assert percent_funded(100000, 200000) == 50
    assert percent_funded(1, 3) == 33  # 33.3
    assert percent_funded(2, 3) == 67  # 66.7
    assert percent_funded(1, 200) == 1  # 0.5 -> 1
    assert percent_funded(300000, 200000) == 150
    assert percent_funded(-1000, 200000) == -1  # -0.5 -> -1
    assert percent_funded(100000, 0) is None
    result = analyse(plan([ComponentInput("New", 100, useful_life=5, remaining_life=5)]))
    assert result.fully_funded_cents == 0 and result.percent_funded is None


def test_needed_contribution_is_zero_when_never_negative():
    pond = ComponentInput("Pond", 100000, useful_life=10, remaining_life=3)
    assert needed_contribution(plan([pond], starting_balance_cents=100000)) == 0
    assert needed_contribution(plan()) == 0


def test_needed_contribution_spreads_a_later_expense():
    # Nothing in hand, $1,000.00 due in year 3 (the 4th contribution lands first):
    # 4C >= 1000 -> C = $250.00.
    item = ComponentInput("Signs", 100000, useful_life=30, remaining_life=3)
    assert needed_contribution(plan([item])) == 25000
    # $1,000.01 -> 250.0025 -> the next whole cent, $250.01.
    item = ComponentInput("Signs", 100001, useful_life=30, remaining_life=3)
    assert needed_contribution(plan([item])) == 25001


def test_needed_contribution_covers_a_starting_deficit():
    assert needed_contribution(plan(starting_balance_cents=-30000, horizon_years=3)) == 30000


@pytest.mark.parametrize("inflation,interest", [(0, 0), (300, 0), (0, 250), (350, 175)])
def test_needed_contribution_is_the_smallest_that_works(inflation, interest):
    components = [
        ComponentInput("Paving", 18000000, useful_life=25, remaining_life=6),
        ComponentInput("Sealcoat", 1500000, useful_life=5, remaining_life=1),
        ComponentInput("Pond", 4500000, useful_life=20, remaining_life=12),
        ComponentInput("Entrance sign", 800000, useful_life=15, remaining_life=0),
    ]
    inputs = plan(
        components,
        starting_balance_cents=2500000,
        inflation_bp=inflation,
        interest_bp=interest,
        horizon_years=30,
    )
    needed = needed_contribution(inputs)
    assert needed > 0
    assert all(r.end_cents >= 0 for r in project(inputs, needed))
    assert any(r.end_cents < 0 for r in project(inputs, needed - 1))


def test_projection_rows_add_up():
    components = [
        ComponentInput("Paving", 18000000, useful_life=25, remaining_life=6),
        ComponentInput("Sealcoat", 1500000, useful_life=5, remaining_life=1),
    ]
    rows = project(
        plan(
            components,
            starting_balance_cents=2500000,
            annual_contribution_cents=900000,
            inflation_bp=300,
            interest_bp=150,
            horizon_years=30,
        )
    )
    assert len(rows) == 30
    for prev, row in itertools.pairwise(rows):
        assert row.begin_cents == prev.end_cents
    for row in rows:
        assert row.end_cents == (
            row.begin_cents + row.contribution_cents + row.interest_cents - row.expense_cents
        )
        assert all(isinstance(v, int) for v in (row.end_cents, row.interest_cents))


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError):
        ComponentInput("X", 100, useful_life=0, remaining_life=0)
    with pytest.raises(ValueError):
        ComponentInput("X", 100, useful_life=5, remaining_life=-1)
    with pytest.raises(ValueError):
        ComponentInput("X", -1, useful_life=5, remaining_life=1)
    with pytest.raises(ValueError):
        plan(horizon_years=0)
    with pytest.raises(ValueError):
        plan(inflation_bp=-1)


def test_per_lot_monthly_rounds_up():
    # $10,000 a year over 44 lots = $18.9393... a month -> $18.94.
    assert per_lot_monthly(1000000, 44) == 1894
    assert per_lot_monthly(52800, 44) == 100
    assert per_lot_monthly(1000, 0) == 0

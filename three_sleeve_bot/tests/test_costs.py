"""costs.py must stay identical to the research cost model (../cost_model.py)."""
import itertools

import pytest

import cost_model
import costs


@pytest.mark.parametrize("premium,qty,is_buy,day", list(itertools.product(
    [0.05, 3.2, 47.5, 180.0, 912.0], [65, 130, 1950], [True, False],
    ["2022-06-01", "2023-05-01", "2024-11-01", "2026-05-01"])))
def test_option_fill_cost_matches_research(premium, qty, is_buy, day):
    assert costs.option_fill_cost(premium, qty, is_buy, day) == pytest.approx(
        cost_model.option_fill_cost(premium, qty, is_buy, day), abs=1e-9)


@pytest.mark.parametrize("premium,urgent", list(itertools.product([0.05, 12.0, 250.0], [True, False])))
def test_slip_matches_research(premium, urgent):
    assert costs.slip(premium, urgent) == cost_model.slip(premium, urgent)


def test_running_costs_match():
    assert costs.MONTHLY_RUNNING_COSTS == sum(cost_model.MONTHLY_RUNNING_COSTS.values())
    assert costs.LOT == cost_model.get_lot_size("2026-10-20")


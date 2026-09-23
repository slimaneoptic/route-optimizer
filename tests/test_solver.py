import io
import sys
from dataclasses import replace
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export import workbook  # noqa: E402
from roads import cached_sample  # noqa: E402
from solver import Settings, greedy_baseline, load_stops, maps_legs, plan_cost, solve, straight_line_matrices  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
CITIES = ["casablanca", "riyadh", "dubai"]


def _setup(city, **kw):
    s = Settings(vehicles=4, capacity=40, time_limit_s=2, **kw)
    stops = load_stops(pd.read_csv(DATA / f"{city}.csv"), s)
    mx = cached_sample(city, list(zip(stops.lat, stops.lon)))
    assert mx is not None and mx.source == "roads", "bundled road matrix must match the sample CSV"
    return s, stops, mx


def _check_plan(stops, plan, s):
    seen = [n for r in plan.routes for n in r.nodes]
    assert len(seen) == len(set(seen)), "a stop is visited twice"
    assert set(seen) | set(plan.dropped) == set(range(1, len(stops)))
    for r in plan.routes:
        assert r.load <= s.capacity
        assert r.arrivals == sorted(r.arrivals)
        assert r.end <= s.shift_start + s.shift_minutes
        for node, t in zip(r.nodes, r.arrivals):
            assert stops.tw_start[node] <= t <= stops.tw_end[node], f"{stops.name[node]} served outside its window"


@pytest.mark.parametrize("city", CITIES)
def test_optimizer_serves_everyone_on_real_roads_and_beats_manual(city):
    s, stops, mx = _setup(city)
    plan, base = solve(stops, s, mx), greedy_baseline(stops, s, mx)
    assert plan.dropped == []
    _check_plan(stops, plan, s)
    _check_plan(stops, base, s)
    assert plan_cost(plan, s) <= plan_cost(base, s) + 1e-6


def test_straight_line_fallback_still_solves():
    s = Settings(time_limit_s=1)
    stops = load_stops(pd.read_csv(DATA / "casablanca.csv"), s)
    plan = solve(stops, s, straight_line_matrices(stops, 30))
    assert plan.dropped == []
    _check_plan(stops, plan, s)


def test_expensive_vans_mean_fewer_vans():
    s, stops, mx = _setup("dubai")
    cheap = solve(stops, replace(s, cost_per_van=0.0), mx)
    dear = solve(stops, replace(s, cost_per_van=500.0), mx)
    assert len(dear.routes) <= len(cheap.routes)
    assert dear.dropped == []


def test_infeasible_stops_are_dropped_not_crashing():
    s, stops, mx = _setup("casablanca")
    plan = solve(stops, replace(s, vehicles=1, capacity=5, time_limit_s=1), mx)
    assert plan.dropped, "one van of capacity 5 cannot serve 80 units"


def test_excel_has_a_sheet_per_van_and_navigation_links():
    s, stops, mx = _setup("dubai")
    plan = solve(stops, s, mx)
    wb = openpyxl.load_workbook(io.BytesIO(workbook(stops, plan, s, "USD")))
    assert wb.sheetnames == ["Summary"] + [f"Van {r.vehicle}" for r in plan.routes]
    for r in plan.routes:
        legs = maps_legs(stops, r)
        assert all(link.count("%7C") <= 8 for link in legs), "max 9 waypoints per link"
        assert len(legs) == -(-(len(r.nodes) + 1) // 10)  # ceil((stops + return) / 10)


def test_depot_flag_moves_row_to_front_and_errors_are_readable():
    s = Settings()
    df = pd.DataFrame({"name": ["A", "Depot", "B"], "lat": [33.5, 33.6, 33.55], "lon": [-7.6, -7.6, -7.58], "depot": [0, 1, 0]})
    assert load_stops(df, s).name[0] == "Depot"
    with pytest.raises(ValueError, match="lat"):
        load_stops(pd.DataFrame({"name": ["x"], "lon": [1.0]}), s)

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solver import Settings, greedy_baseline, load_stops, solve  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
CITIES = [("casablanca", 30), ("riyadh", 40), ("dubai", 40)]


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


@pytest.mark.parametrize("city,speed", CITIES)
def test_optimizer_serves_everyone_within_rules(city, speed):
    s = Settings(vehicles=4, capacity=40, speed_kmh=speed, time_limit_s=2)
    stops = load_stops(pd.read_csv(DATA / f"{city}.csv"), s)
    plan = solve(stops, s)
    assert plan.dropped == []
    _check_plan(stops, plan, s)


@pytest.mark.parametrize("city,speed", CITIES)
def test_baseline_respects_rules_and_is_not_better(city, speed):
    s = Settings(vehicles=4, capacity=40, speed_kmh=speed, time_limit_s=2)
    stops = load_stops(pd.read_csv(DATA / f"{city}.csv"), s)
    base = greedy_baseline(stops, s)
    _check_plan(stops, base, s)
    assert solve(stops, s).total_km <= base.total_km + 1e-6


def test_infeasible_stops_are_dropped_not_crashing():
    s = Settings(vehicles=1, capacity=5, speed_kmh=30, time_limit_s=1)
    stops = load_stops(pd.read_csv(DATA / "casablanca.csv"), s)
    plan = solve(stops, s)
    assert plan.dropped, "one van of capacity 5 cannot serve 80 units"
    _check_plan(stops, plan, s)


def test_depot_flag_moves_row_to_front_and_errors_are_readable():
    s = Settings()
    df = pd.DataFrame({"name": ["A", "Depot", "B"], "lat": [33.5, 33.6, 33.55], "lon": [-7.6, -7.6, -7.58], "depot": [0, 1, 0]})
    assert load_stops(df, s).name[0] == "Depot"
    with pytest.raises(ValueError, match="lat"):
        load_stops(pd.DataFrame({"name": ["x"], "lon": [1.0]}), s)

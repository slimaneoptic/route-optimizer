"""Vehicle routing (capacities + time windows + shift length) on Google OR-Tools.

Pure functions, no UI: load stops, build matrices, solve, and a greedy baseline
that mimics quick manual planning ("always drive to the nearest next customer").
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import pandas as pd
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

ROAD_FACTOR = 1.3  # straight-line km -> approximate road km in a city
DROP_PENALTY_M = 1_000_000  # cost of leaving a stop unserved (only when infeasible)


@dataclass
class Settings:
    vehicles: int = 4
    capacity: int = 40
    speed_kmh: float = 30.0
    shift_start: int = 8 * 60  # minutes from midnight
    shift_minutes: int = 9 * 60
    use_time_windows: bool = True
    balance: bool = False
    time_limit_s: int = 5


@dataclass
class Route:
    vehicle: int
    nodes: list[int]  # stop indices in visiting order, depot excluded
    arrivals: list[int]  # minutes from midnight, one per node
    distance_km: float
    load: int
    start: int
    end: int


@dataclass
class Plan:
    routes: list[Route]
    dropped: list[int]
    total_km: float
    solve_s: float
    status: str

    @property
    def served(self) -> int:
        return sum(len(r.nodes) for r in self.routes)


# ---------------------------------------------------------------- input


def _to_minutes(value, default: int) -> int:
    if value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == "":
        return default
    text = str(value).strip()
    if ":" in text:
        h, m = text.split(":")[:2]
        return int(h) * 60 + int(m)
    return int(float(text))


def load_stops(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Normalize an uploaded table. Row flagged depot=1 (or the first row) becomes index 0."""
    cols = {c.lower().strip(): c for c in df.columns}
    missing = [c for c in ("lat", "lon") if c not in cols]
    if missing:
        raise ValueError(f"Missing column(s): {', '.join(missing)}. Required: name, lat, lon.")

    out = pd.DataFrame()
    out["name"] = df[cols["name"]].astype(str) if "name" in cols else [f"Stop {i}" for i in range(len(df))]
    out["lat"] = pd.to_numeric(df[cols["lat"]], errors="coerce")
    out["lon"] = pd.to_numeric(df[cols["lon"]], errors="coerce")
    if out[["lat", "lon"]].isna().any().any():
        bad = out[out[["lat", "lon"]].isna().any(axis=1)].index.tolist()
        raise ValueError(f"Rows with invalid coordinates: {[i + 2 for i in bad]} (spreadsheet row numbers).")

    out["demand"] = pd.to_numeric(df[cols["demand"]], errors="coerce").fillna(1).astype(int) if "demand" in cols else 1
    out["service_min"] = (
        pd.to_numeric(df[cols["service_min"]], errors="coerce").fillna(5).astype(int) if "service_min" in cols else 5
    )
    day_end = settings.shift_start + settings.shift_minutes
    out["tw_start"] = [_to_minutes(v, settings.shift_start) for v in df[cols["tw_start"]]] if "tw_start" in cols else settings.shift_start
    out["tw_end"] = [_to_minutes(v, day_end) for v in df[cols["tw_end"]]] if "tw_end" in cols else day_end

    depot_idx = 0
    if "depot" in cols:
        flagged = df.index[pd.to_numeric(df[cols["depot"]], errors="coerce").fillna(0) > 0].tolist()
        if flagged:
            depot_idx = df.index.get_loc(flagged[0])
    order = [depot_idx] + [i for i in range(len(out)) if i != depot_idx]
    out = out.iloc[order].reset_index(drop=True)
    out.loc[0, ["demand", "service_min"]] = 0
    if len(out) < 2:
        raise ValueError("Need a depot and at least one stop.")
    return out


# ---------------------------------------------------------------- matrices


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_matrix_m(stops: pd.DataFrame) -> list[list[int]]:
    pts = list(zip(stops.lat, stops.lon))
    return [[int(haversine_km(*a, *b) * ROAD_FACTOR * 1000) for b in pts] for a in pts]


def travel_minutes(dist_m: list[list[int]], speed_kmh: float) -> list[list[int]]:
    return [[int(round(d / 1000 / speed_kmh * 60)) for d in row] for row in dist_m]


# ---------------------------------------------------------------- optimizer


def solve(stops: pd.DataFrame, s: Settings) -> Plan:
    n = len(stops)
    dist = distance_matrix_m(stops)
    travel = travel_minutes(dist, s.speed_kmh)
    service = stops.service_min.tolist()
    demand = stops.demand.tolist()
    day_end = s.shift_start + s.shift_minutes

    manager = pywrapcp.RoutingIndexManager(n, s.vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def dist_cb(i, j):
        return dist[manager.IndexToNode(i)][manager.IndexToNode(j)]

    def time_cb(i, j):
        a, b = manager.IndexToNode(i), manager.IndexToNode(j)
        return service[a] + travel[a][b]

    def demand_cb(i):
        return demand[manager.IndexToNode(i)]

    dist_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(dist_idx)

    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(demand_cb), 0, [s.capacity] * s.vehicles, True, "Load"
    )

    time_idx = routing.RegisterTransitCallback(time_cb)
    routing.AddDimension(time_idx, 120, 24 * 60, False, "Time")  # up to 2h waiting at a stop
    tdim = routing.GetDimensionOrDie("Time")
    for node in range(1, n):
        idx = manager.NodeToIndex(node)
        lo, hi = (int(stops.tw_start[node]), int(stops.tw_end[node])) if s.use_time_windows else (s.shift_start, day_end)
        lo, hi = max(lo, s.shift_start), max(min(hi, day_end), s.shift_start)
        tdim.CumulVar(idx).SetRange(min(lo, hi), hi)
        routing.AddDisjunction([idx], DROP_PENALTY_M)
    for v in range(s.vehicles):
        tdim.CumulVar(routing.Start(v)).SetRange(s.shift_start, day_end)
        tdim.CumulVar(routing.End(v)).SetRange(s.shift_start, day_end)
        routing.AddVariableMinimizedByFinalizer(tdim.CumulVar(routing.End(v)))

    if s.balance:
        routing.AddDimension(dist_idx, 0, 10_000_000, True, "Distance")
        routing.GetDimensionOrDie("Distance").SetGlobalSpanCostCoefficient(50)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(max(1, int(s.time_limit_s)))

    t0 = time.perf_counter()
    sol = routing.SolveWithParameters(params)
    elapsed = time.perf_counter() - t0
    if sol is None:
        return Plan([], list(range(1, n)), 0.0, elapsed, "NO SOLUTION")

    routes, visited = [], set()
    for v in range(s.vehicles):
        idx = routing.Start(v)
        start = sol.Min(tdim.CumulVar(idx))
        nodes, arrivals, meters, load = [], [], 0, 0
        while not routing.IsEnd(idx):
            nxt = sol.Value(routing.NextVar(idx))
            meters += dist[manager.IndexToNode(idx)][manager.IndexToNode(nxt)]
            idx = nxt
            node = manager.IndexToNode(idx)
            if not routing.IsEnd(idx):
                nodes.append(node)
                arrivals.append(sol.Min(tdim.CumulVar(idx)))
                load += demand[node]
        end = sol.Min(tdim.CumulVar(idx))
        if nodes:
            routes.append(Route(v + 1, nodes, arrivals, meters / 1000, load, start, end))
            visited.update(nodes)
    dropped = [i for i in range(1, n) if i not in visited]
    return Plan(routes, dropped, sum(r.distance_km for r in routes), elapsed, "OK")


# ---------------------------------------------------------------- baseline


def greedy_baseline(stops: pd.DataFrame, s: Settings) -> Plan:
    """Quick manual-style plan: fill one van at a time, always driving to the nearest
    stop that still fits. Like a human planner, it adds another van when stops are left."""
    n = len(stops)
    dist = distance_matrix_m(stops)
    travel = travel_minutes(dist, s.speed_kmh)
    day_end = s.shift_start + s.shift_minutes
    left = set(range(1, n))
    routes: list[Route] = []
    while left and len(routes) < 3 * s.vehicles:
        here, clock, load, meters = 0, s.shift_start, 0, 0
        nodes, arrivals = [], []
        while True:
            best = None
            for j in sorted(left, key=lambda j: dist[here][j]):
                arrive = clock + stops.service_min[here] + travel[here][j]
                lo, hi = (stops.tw_start[j], stops.tw_end[j]) if s.use_time_windows else (s.shift_start, day_end)
                begin = max(arrive, lo)
                back = begin + stops.service_min[j] + travel[j][0]
                waits_ok = not nodes or begin - arrive <= 120  # a van can leave the depot later
                if load + stops.demand[j] <= s.capacity and begin <= hi and waits_ok and back <= day_end:
                    best = (j, begin)
                    break
            if best is None:
                break
            j, clock = best
            meters += dist[here][j]
            load += int(stops.demand[j])
            nodes.append(j)
            arrivals.append(int(clock))
            left.discard(j)
            here = j
        if not nodes:
            break  # remaining stops cannot be served even by an empty van
        meters += dist[here][0]
        end = clock + stops.service_min[here] + travel[here][0]
        routes.append(Route(len(routes) + 1, nodes, arrivals, meters / 1000, load, s.shift_start, int(end)))
    return Plan(routes, sorted(left), sum(r.distance_km for r in routes), 0.0, "OK")


# ---------------------------------------------------------------- output


def hhmm(minutes: int) -> str:
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


def plan_table(stops: pd.DataFrame, plan: Plan) -> pd.DataFrame:
    rows = []
    for r in plan.routes:
        for seq, (node, t) in enumerate(zip(r.nodes, r.arrivals), start=1):
            rows.append(
                {
                    "vehicle": r.vehicle,
                    "sequence": seq,
                    "stop": stops.name[node],
                    "arrival": hhmm(t),
                    "window": f"{hhmm(stops.tw_start[node])}–{hhmm(stops.tw_end[node])}",
                    "demand": int(stops.demand[node]),
                    "lat": stops.lat[node],
                    "lon": stops.lon[node],
                }
            )
    return pd.DataFrame(rows)

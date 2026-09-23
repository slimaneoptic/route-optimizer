"""Vehicle routing (capacities + time windows + shift length) on Google OR-Tools, optimized for cost.

Pure functions, no UI: load stops, build matrices, solve, a fleet-size sweep, and a greedy
baseline that mimics quick manual planning ("always drive to the nearest next customer").
Costs: every km costs `cost_per_km`, every van used costs `cost_per_van` per day, so the solver
itself decides whether one more van pays off.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

import pandas as pd
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from roads import Matrices

ROAD_FACTOR = 1.3  # straight-line km -> approximate road km, only when road data is unavailable
DROP_PENALTY = 10**9  # cost of leaving a stop unserved (milli-currency); only used when infeasible
MAX_WAIT_MIN = 120


@dataclass
class Settings:
    vehicles: int = 4
    capacity: int = 40
    speed_kmh: float = 30.0  # straight-line fallback only
    traffic_factor: float = 1.3  # multiplies free-flow road drive times
    shift_start: int = 8 * 60  # minutes from midnight
    shift_minutes: int = 9 * 60
    use_time_windows: bool = True
    balance: bool = False
    time_limit_s: int = 5
    cost_per_km: float = 0.30
    cost_per_van: float = 80.0  # per van per day: driver + vehicle


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


def plan_cost(plan: Plan, s: Settings) -> float:
    return plan.total_km * s.cost_per_km + len(plan.routes) * s.cost_per_van


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
    cols = {str(c).lower().strip(): c for c in df.columns}
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
    if len(out) > 200:
        raise ValueError("This demo handles up to 200 stops. Client versions handle thousands.")

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


def straight_line_matrices(stops: pd.DataFrame, speed_kmh: float) -> Matrices:
    pts = list(zip(stops.lat, stops.lon))
    dist = [[int(haversine_km(*a, *b) * ROAD_FACTOR * 1000) for b in pts] for a in pts]
    secs = [[int(d / 1000 / speed_kmh * 3600) for d in row] for row in dist]
    return Matrices(dist, secs, "straight-line")


def travel_minutes(mx: Matrices, s: Settings) -> list[list[int]]:
    factor = s.traffic_factor if mx.source == "roads" else 1.0
    return [[int(round(t * factor / 60)) for t in row] for row in mx.time_s]


# ---------------------------------------------------------------- optimizer


def solve(stops: pd.DataFrame, s: Settings, mx: Matrices) -> Plan:
    n = len(stops)
    dist = mx.dist_m
    travel = travel_minutes(mx, s)
    service = stops.service_min.tolist()
    demand = stops.demand.tolist()
    day_end = s.shift_start + s.shift_minutes
    per_m = max(s.cost_per_km, 0.01)  # milli-currency per meter == currency per km
    arc_cost = [[int(d * per_m) for d in row] for row in dist]

    manager = pywrapcp.RoutingIndexManager(n, s.vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def cost_cb(i, j):
        return arc_cost[manager.IndexToNode(i)][manager.IndexToNode(j)]

    def dist_cb(i, j):
        return dist[manager.IndexToNode(i)][manager.IndexToNode(j)]

    def time_cb(i, j):
        a, b = manager.IndexToNode(i), manager.IndexToNode(j)
        return service[a] + travel[a][b]

    def demand_cb(i):
        return demand[manager.IndexToNode(i)]

    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(cost_cb))
    routing.SetFixedCostOfAllVehicles(int(s.cost_per_van * 1000))

    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(demand_cb), 0, [s.capacity] * s.vehicles, True, "Load"
    )

    routing.AddDimension(routing.RegisterTransitCallback(time_cb), MAX_WAIT_MIN, 24 * 60, False, "Time")
    tdim = routing.GetDimensionOrDie("Time")
    for node in range(1, n):
        idx = manager.NodeToIndex(node)
        lo, hi = (int(stops.tw_start[node]), int(stops.tw_end[node])) if s.use_time_windows else (s.shift_start, day_end)
        lo, hi = max(lo, s.shift_start), max(min(hi, day_end), s.shift_start)
        tdim.CumulVar(idx).SetRange(min(lo, hi), hi)
        routing.AddDisjunction([idx], DROP_PENALTY)
    for v in range(s.vehicles):
        tdim.CumulVar(routing.Start(v)).SetRange(s.shift_start, day_end)
        tdim.CumulVar(routing.End(v)).SetRange(s.shift_start, day_end)
        routing.AddVariableMinimizedByFinalizer(tdim.CumulVar(routing.End(v)))

    if s.balance:
        routing.AddDimension(routing.RegisterTransitCallback(dist_cb), 0, 10**8, True, "Distance")
        routing.GetDimensionOrDie("Distance").SetGlobalSpanCostCoefficient(max(1, int(50 * per_m)))

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
            routes.append(Route(len(routes) + 1, nodes, arrivals, meters / 1000, load, start, end))
            visited.update(nodes)
    dropped = [i for i in range(1, n) if i not in visited]
    return Plan(routes, dropped, sum(r.distance_km for r in routes), elapsed, "OK")


def fleet_sweep(stops: pd.DataFrame, s: Settings, mx: Matrices, max_vans: int) -> pd.DataFrame:
    """Solve once per fleet size: what does each extra van buy?"""
    rows = []
    for v in range(1, max_vans + 1):
        p = solve(stops, replace(s, vehicles=v, time_limit_s=1), mx)
        rows.append({"vans available": v, "vans used": len(p.routes), "customers served": p.served,
                     "not served": len(p.dropped), "km": round(p.total_km, 1), "daily cost": round(plan_cost(p, s), 2)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- baseline


def greedy_baseline(stops: pd.DataFrame, s: Settings, mx: Matrices) -> Plan:
    """Quick manual-style plan: fill one van at a time, always driving to the nearest
    stop that still fits. Like a human planner, it adds another van when stops are left."""
    n = len(stops)
    dist = mx.dist_m
    travel = travel_minutes(mx, s)
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
                waits_ok = not nodes or begin - arrive <= MAX_WAIT_MIN  # a van can leave the depot later
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


def maps_legs(stops: pd.DataFrame, route: Route, per_leg: int = 9) -> list[str]:
    """Google Maps driving links for a route, split into legs of at most `per_leg` waypoints."""
    pts = [(stops.lat[0], stops.lon[0])] + [(stops.lat[n], stops.lon[n]) for n in route.nodes] + [(stops.lat[0], stops.lon[0])]
    links, i = [], 0
    while i < len(pts) - 1:
        chunk = pts[i : i + per_leg + 2]
        fmt = lambda p: f"{p[0]:.5f},{p[1]:.5f}"  # noqa: E731
        url = f"https://www.google.com/maps/dir/?api=1&travelmode=driving&origin={fmt(chunk[0])}&destination={fmt(chunk[-1])}"
        if len(chunk) > 2:
            url += "&waypoints=" + "%7C".join(fmt(p) for p in chunk[1:-1])
        links.append(url)
        i += len(chunk) - 1
    return links


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

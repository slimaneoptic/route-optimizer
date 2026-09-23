"""Route optimizer demo: upload stops, set the fleet and costs, get optimized delivery routes."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from export import workbook
from roads import cached_sample, osrm_table, route_shape
from solver import Settings, fleet_sweep, greedy_baseline, hhmm, load_stops, maps_legs, plan_cost, plan_table, solve, straight_line_matrices

AUTHOR = "Slimane"
HIRE_URL = "https://www.upwork.com/freelancers/~01d6ae0f1a06a43ac0"

DATA = Path(__file__).parent / "data"
CITIES = {"Casablanca, 25 stops": "casablanca", "Riyadh, 40 stops": "riyadh", "Dubai, 30 stops": "dubai"}
CURRENCIES = ["USD", "EUR", "MAD", "SAR", "AED"]
# Validated categorical palette, fixed order. Light fills (aqua, yellow, pink) get dark numbers.
VAN_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DARK_TEXT = {"#1baf7a", "#eda100", "#e87ba4"}

st.set_page_config(page_title="Route optimizer", page_icon="🚚", layout="wide")
st.markdown(
    """<style>
    .block-container {padding-top: 2rem; max-width: 1280px;}
    [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
    .van-row {display:flex; align-items:flex-start; gap:.6rem; padding:.5rem 0; border-bottom:1px solid #D5DBE1; font-variant-numeric: tabular-nums;}
    .van-dot {width:14px; height:14px; border-radius:50%; flex:none; margin-top:.2rem; box-shadow:0 0 0 2px #fff, 0 0 0 3px #9aa6b2;}
    .van-name {font-weight:600; width:4.2rem; flex:none;}
    .van-meta {color:#4B5866; font-size:.92rem;}
    .van-meta a {color:#1F5FAD; white-space:nowrap;}
    .savings {background:#E9F4EC; border-left:4px solid #0ca30c; padding:.8rem 1rem; border-radius:6px; margin:.4rem 0 1rem; font-size:1.02rem;}
    .footer {margin-top:2.5rem; padding-top:1rem; border-top:1px solid #D5DBE1; color:#4B5866; font-size:.92rem;}
    </style>""",
    unsafe_allow_html=True,
)


def color(v: int) -> str:
    return VAN_COLORS[(v - 1) % len(VAN_COLORS)]


@st.cache_data
def read_table(source, name: str) -> pd.DataFrame:
    return pd.read_excel(source) if name.lower().endswith((".xlsx", ".xls")) else pd.read_csv(source)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def road_matrices(coords: tuple):
    return osrm_table(list(coords))


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def road_shape(points: tuple):
    return route_shape(list(points))


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Stops")
    choice = st.selectbox("Sample data", list(CITIES), index=0)
    upload = st.file_uploader("Or upload your own (CSV or Excel)", type=["csv", "xlsx"], help="Columns: name, lat, lon, and optionally demand, tw_start, tw_end (HH:MM), service_min, depot (1 = depot).")
    st.download_button("Download template", (DATA / "casablanca.csv").read_bytes(), "stops_template.csv", "text/csv")

    st.header("Fleet")
    vehicles = st.slider("Vans available", 1, 8, 4)
    capacity = st.slider("Capacity per van (units)", 5, 120, 40, step=5)
    start_t = st.time_input("Shift starts", dt.time(8, 0), step=1800)
    shift_h = st.slider("Max shift length (hours)", 4, 12, 9)

    st.header("Roads")
    use_roads = st.toggle("Real road distances", True, help="Road network from OpenStreetMap (OSRM). Off = straight-line estimate.")
    if use_roads:
        traffic = st.slider("Traffic factor", 1.0, 2.0, 1.3, step=0.1, help="Multiplies free-flow drive times. 1.3 ≈ normal city traffic.")
        speed = 30
    else:
        speed = st.slider("Average speed (km/h)", 15, 70, 30, step=5)
        traffic = 1.0

    st.header("Costs")
    currency = st.selectbox("Currency", CURRENCIES, index=0)
    cost_km = st.number_input(f"Cost per km ({currency})", 0.05, 50.0, 0.30, step=0.05, help="Fuel, tyres, maintenance.")
    cost_van = st.number_input(f"Cost per van per day ({currency})", 0.0, 5000.0, 80.0, step=10.0, help="Driver wage + vehicle cost for a working day.")
    days = st.number_input("Working days per year", 100, 365, 300, step=10)

    st.header("Rules")
    use_tw = st.toggle("Respect delivery windows", True)
    balance = st.toggle("Balance work between vans", False, help="Shorter longest route, at the cost of a few extra km in total.")
    limit = st.slider("Search time (seconds)", 1, 20, 3)

    run = st.button("Optimize routes", type="primary", width="stretch")

settings = Settings(
    vehicles=vehicles, capacity=capacity, speed_kmh=float(speed), traffic_factor=float(traffic),
    shift_start=start_t.hour * 60 + start_t.minute, shift_minutes=shift_h * 60,
    use_time_windows=use_tw, balance=balance, time_limit_s=limit, cost_per_km=float(cost_km), cost_per_van=float(cost_van),
)

# ---------------------------------------------------------------- data
st.title("Route optimizer")
st.markdown(
    "Give it your stops, your fleet and your costs. It plans the cheapest routes that respect van capacity, "
    "delivery windows and shift length on real roads, and compares them with a quick manual plan."
)

try:
    raw = read_table(upload, upload.name) if upload is not None else read_table(DATA / f"{CITIES[choice]}.csv", "x.csv")
    stops = load_stops(raw, settings)
except ValueError as err:
    st.error(f"Couldn't read the file. {err}")
    st.stop()

coords = tuple(zip(stops.lat.round(5), stops.lon.round(5)))
key = (upload.name if upload else choice, use_roads, tuple(vars(settings).items()))
if run or ("plan" not in st.session_state):
    with st.spinner("Loading road network and searching for the cheapest routes…"):
        mx, road_note = None, None
        if use_roads:
            mx = cached_sample(CITIES[choice], list(coords)) if upload is None else None
            if mx is None:
                try:
                    mx = road_matrices(coords)
                except Exception:
                    road_note = "Road data is unavailable right now, so distances are straight-line estimates."
        if mx is None:
            mx = straight_line_matrices(stops, settings.speed_kmh)
        st.session_state.update(
            plan=solve(stops, settings, mx), base=greedy_baseline(stops, settings, mx), mx=mx, key=key,
            stops=stops, settings=settings, currency=currency, days=days, road_note=road_note, sweep=None,
        )

stale = st.session_state.key != key
plan, base, mx = st.session_state.plan, st.session_state.base, st.session_state.mx
shown, used = st.session_state.stops, st.session_state.settings
cur, yr_days = st.session_state.currency, st.session_state.days
if stale:
    st.info("Settings changed. Press **Optimize routes** to update the plan.")
if st.session_state.road_note:
    st.warning(st.session_state.road_note)

# ---------------------------------------------------------------- KPIs
n_stops = len(shown) - 1
cost, base_cost = plan_cost(plan, used), plan_cost(base, used)
saved = base_cost - cost
km_saved = base.total_km - plan.total_km
c1, c2, c3, c4 = st.columns(4)
c1.metric("Daily cost", f"{cost:,.0f} {cur}", f"{-saved:,.0f} {cur} vs manual ({saved / base_cost * 100:.0f}% less)" if saved > 0.5 else None, delta_color="inverse")
c2.metric("Distance", f"{plan.total_km:,.0f} km", f"{-km_saved:,.0f} km vs manual" if km_saved > 0.5 else None, delta_color="inverse")
c3.metric("Vans used", f"{len(plan.routes)}", f"{len(plan.routes) - len(base.routes):+d} vs manual" if len(base.routes) != len(plan.routes) else None, delta_color="inverse")
c4.metric("Customers served", f"{plan.served} / {n_stops}", f"{plan.served - base.served:+d} vs manual" if plan.served != base.served else None)

if saved > 0.5 and plan.served >= base.served:
    st.markdown(
        f'<div class="savings">≈ <b>{saved * yr_days:,.0f} {cur} saved per year</b> compared with the manual plan '
        f"({yr_days} working days), with every rule respected. Found in {plan.solve_s:.0f} s, "
        f'on {"real roads" if mx.source == "roads" else "straight-line distances"}.</div>',
        unsafe_allow_html=True,
    )
if plan.dropped:
    names = ", ".join(shown.name[i] for i in plan.dropped[:8])
    st.warning(f"{len(plan.dropped)} stop(s) can't fit with this fleet and these rules: {names}{'…' if len(plan.dropped) > 8 else ''}. Add a van, raise capacity or extend the shift.")

# ---------------------------------------------------------------- map + vans
left, right = st.columns([2.2, 1], gap="large")
with left:
    m = folium.Map(location=[shown.lat.mean(), shown.lon.mean()], zoom_start=12, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", opacity=0.55).add_to(m)  # faded so the routes carry the color
    m.fit_bounds([[shown.lat.min(), shown.lon.min()], [shown.lat.max(), shown.lon.max()]], padding=(20, 20))
    depot = shown.iloc[0]
    folium.Marker(
        [depot.lat, depot.lon],
        tooltip=f"Depot: {depot['name']}",
        icon=folium.DivIcon(html='<div style="background:#14202B;color:#fff;font:700 10px/22px Archivo,sans-serif;width:30px;height:22px;text-align:center;border-radius:4px;box-shadow:0 0 0 2px #fff">HQ</div>', icon_size=(30, 22), icon_anchor=(15, 11)),
    ).add_to(m)
    for r in plan.routes:
        c = color(r.vehicle)
        pts = tuple([(depot.lat, depot.lon)] + [(shown.lat[n], shown.lon[n]) for n in r.nodes] + [(depot.lat, depot.lon)])
        path = road_shape(pts) if mx.source == "roads" else None
        folium.PolyLine(path or [list(p) for p in pts], color=c, weight=4 if path else 3, opacity=0.85, tooltip=f"Van {r.vehicle}: {r.distance_km:.1f} km").add_to(m)
        ink = "#14202B" if c in DARK_TEXT else "#FFFFFF"
        for seq, (n, t) in enumerate(zip(r.nodes, r.arrivals), start=1):
            tip = f"Van {r.vehicle}, stop {seq}: {shown.name[n]} · arrive {hhmm(t)} · window {hhmm(shown.tw_start[n])}–{hhmm(shown.tw_end[n])} · {int(shown.demand[n])} units"
            folium.Marker(
                [shown.lat[n], shown.lon[n]],
                tooltip=tip,
                icon=folium.DivIcon(html=f'<div style="background:{c};color:{ink};font:700 10px/20px Archivo,sans-serif;width:20px;height:20px;border-radius:50%;text-align:center;box-shadow:0 0 0 2px #fff,0 0 0 3px rgba(20,32,43,.35)">{seq}</div>', icon_size=(20, 20), icon_anchor=(10, 10)),
            ).add_to(m)
    for n in plan.dropped:
        folium.CircleMarker([shown.lat[n], shown.lon[n]], radius=7, color="#14202B", weight=2, fill=True, fill_color="#fff", tooltip=f"Not served: {shown.name[n]}").add_to(m)
    st_folium(m, height=560, use_container_width=True, returned_objects=[])

with right:
    st.subheader("Vans")
    rows = []
    for r in plan.routes:
        legs = maps_legs(shown, r)
        nav = " · ".join(f'<a href="{u}" target="_blank">navigate{f" {i}" if len(legs) > 1 else ""}</a>' for i, u in enumerate(legs, start=1))
        rows.append(
            f'<div class="van-row"><span class="van-dot" style="background:{color(r.vehicle)}"></span>'
            f'<span class="van-name">Van {r.vehicle}</span>'
            f'<span class="van-meta">{len(r.nodes)} stops · {r.distance_km:.1f} km · {r.load}/{used.capacity} units<br>'
            f"{hhmm(r.start)}–{hhmm(r.end)} · {r.distance_km * used.cost_per_km + used.cost_per_van:,.0f} {cur} · {nav}</span></div>"
        )
    st.markdown("".join(rows) or "No routes.", unsafe_allow_html=True)
    st.caption(
        f"**Manual plan used for comparison:** fill one van at a time, always driving to the nearest stop that still fits, "
        f"adding a van when needed. Same rules and roads. It needed {len(base.routes)} vans, {base.total_km:,.0f} km, {base_cost:,.0f} {cur}/day."
    )

# ---------------------------------------------------------------- details
table = plan_table(shown, plan)
tab_plan, tab_fleet, tab_how = st.tabs(["Stop-by-stop plan", "How many vans do I need?", "How this works"])
with tab_plan:
    st.dataframe(table.drop(columns=["lat", "lon"]), hide_index=True, width="stretch", height=320)
    d1, d2, _ = st.columns([1.3, 1, 3])
    d1.download_button("Driver sheets (Excel)", workbook(shown, plan, used, cur), "route_plan.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    d2.download_button("Plan (CSV)", table.to_csv(index=False).encode(), "route_plan.csv", "text/csv", width="stretch")

with tab_fleet:
    st.markdown("Solve the same day with 1, 2, 3… vans to see what each extra van buys: more customers served, or lower cost.")
    if st.button("Compare fleet sizes"):
        with st.spinner("Solving once per fleet size…"):
            st.session_state.sweep = fleet_sweep(shown, used, mx, max(vehicles, 2))
    sweep = st.session_state.get("sweep")
    if sweep is not None:
        full = sweep[sweep["not served"] == 0]
        if len(full):
            best = full.sort_values("daily cost").iloc[0]
            st.success(f"Smallest fleet that serves everyone: **{int(best['vans used'])} vans**, {best['daily cost']:,.0f} {cur}/day.")
        else:
            st.warning("Even the largest fleet tested leaves customers unserved. Raise capacity or extend the shift.")
        st.dataframe(sweep.rename(columns={"daily cost": f"daily cost ({cur})"}), hide_index=True, width="stretch")

with tab_how:
    st.markdown(
        """
- **Solver:** Google OR-Tools vehicle routing with guided local search, minimizing total daily cost (km × cost per km + a daily cost per van used). It decides by itself when one more van pays off.
- **Rules it enforces:** van capacity, delivery time windows, maximum shift length, up to 2 hours of waiting at a stop, every van starts and ends at the depot.
- **Roads:** real road distances and drive times from OpenStreetMap (OSRM), multiplied by the traffic factor. Routes on the map follow the roads.
- **For drivers:** one Excel sheet per van with arrival times and map links, plus Google Maps navigation links.
- **Client versions:** your own road/traffic data (Google Maps, HERE), multiple depots, driver breaks, vehicle types, pickup & delivery, re-planning during the day, API or ERP integration.
"""
    )

st.markdown(
    f'<div class="footer">Built by {AUTHOR}. I build routing and scheduling tools for operations teams: '
    f'your rules, your data, running in days. <a href="{HIRE_URL}" target="_blank">Work with me</a></div>',
    unsafe_allow_html=True,
)

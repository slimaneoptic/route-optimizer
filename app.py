"""Route optimizer demo: upload stops, set the fleet, get optimized delivery routes."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from solver import ROAD_FACTOR, Settings, greedy_baseline, hhmm, load_stops, plan_table, solve

# ---- edit these two lines -------------------------------------------------
AUTHOR = "Slimane"
HIRE_URL = "https://www.upwork.com/freelancers/"  # your Upwork profile link
# ---------------------------------------------------------------------------

DATA = Path(__file__).parent / "data"
CITIES = {"Casablanca, 25 stops": ("casablanca.csv", 30), "Riyadh, 40 stops": ("riyadh.csv", 40), "Dubai, 30 stops": ("dubai.csv", 40)}
# Validated categorical palette, fixed order. Light fills (aqua, yellow, pink) get dark numbers.
VAN_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DARK_TEXT = {"#1baf7a", "#eda100", "#e87ba4"}

st.set_page_config(page_title="Route optimizer", page_icon="🚚", layout="wide")
st.markdown(
    """<style>
    .block-container {padding-top: 2rem; max-width: 1280px;}
    [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
    .van-row {display:flex; align-items:center; gap:.6rem; padding:.45rem 0; border-bottom:1px solid #D5DBE1; font-variant-numeric: tabular-nums;}
    .van-dot {width:14px; height:14px; border-radius:50%; flex:none; box-shadow:0 0 0 2px #fff, 0 0 0 3px #9aa6b2;}
    .van-name {font-weight:600; width:4.2rem;}
    .van-meta {color:#4B5866; font-size:.92rem;}
    .footer {margin-top:2.5rem; padding-top:1rem; border-top:1px solid #D5DBE1; color:#4B5866; font-size:.92rem;}
    </style>""",
    unsafe_allow_html=True,
)


def color(v: int) -> str:
    return VAN_COLORS[(v - 1) % len(VAN_COLORS)]


@st.cache_data
def read_csv(source) -> pd.DataFrame:
    return pd.read_csv(source)


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Stops")
    choice = st.selectbox("Sample data", list(CITIES), index=0)
    upload = st.file_uploader("Or upload your own CSV", type=["csv"], help="Columns: name, lat, lon, and optionally demand, tw_start, tw_end (HH:MM), service_min, depot (1 = depot).")
    st.download_button("Download CSV template", (DATA / "casablanca.csv").read_bytes(), "stops_template.csv", "text/csv")

    st.header("Fleet")
    vehicles = st.slider("Vans available", 1, 8, 4)
    capacity = st.slider("Capacity per van (units)", 5, 120, 40, step=5)
    default_speed = CITIES[choice][1]
    speed = st.slider("Average speed (km/h)", 15, 70, default_speed, step=5)
    start_t = st.time_input("Shift starts", dt.time(8, 0), step=1800)
    shift_h = st.slider("Max shift length (hours)", 4, 12, 9)

    st.header("Rules")
    use_tw = st.toggle("Respect delivery windows", True)
    balance = st.toggle("Balance work between vans", False, help="Shorter longest route, at the cost of a few extra km in total.")
    limit = st.slider("Search time (seconds)", 1, 20, 3)

    run = st.button("Optimize routes", type="primary", width="stretch")

settings = Settings(
    vehicles=vehicles,
    capacity=capacity,
    speed_kmh=float(speed),
    shift_start=start_t.hour * 60 + start_t.minute,
    shift_minutes=shift_h * 60,
    use_time_windows=use_tw,
    balance=balance,
    time_limit_s=limit,
)

# ---------------------------------------------------------------- data
st.title("Route optimizer")
st.markdown(
    "Give it your stops and your fleet. It finds the shortest routes that respect van capacity, "
    "delivery windows and shift length, then compares them with a quick manual plan."
)

try:
    raw = read_csv(upload) if upload is not None else read_csv(DATA / CITIES[choice][0])
    stops = load_stops(raw, settings)
except ValueError as err:
    st.error(f"Couldn't read the CSV. {err}")
    st.stop()

key = (upload.name if upload else choice, tuple(vars(settings).items()))
if run or st.session_state.get("key") != key and "plan" not in st.session_state:
    with st.spinner("Searching for the best routes…"):
        st.session_state.plan = solve(stops, settings)
        st.session_state.base = greedy_baseline(stops, settings)
        st.session_state.key = key
        st.session_state.stops = stops

stale = st.session_state.get("key") != key
plan = st.session_state.plan
base = st.session_state.base
shown = st.session_state.stops
if stale:
    st.info("Settings changed. Press **Optimize routes** to update the plan.")

# ---------------------------------------------------------------- KPIs
n_stops = len(shown) - 1
km_saved = base.total_km - plan.total_km
pct = km_saved / base.total_km * 100 if base.total_km else 0
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total distance", f"{plan.total_km:,.0f} km", f"{-km_saved:,.0f} km vs manual ({pct:.0f}% less)" if km_saved > 0.5 else None, delta_color="inverse")
c2.metric("Vans used", f"{len(plan.routes)}", f"{len(plan.routes) - len(base.routes):+d} vs manual" if len(base.routes) != len(plan.routes) else None, delta_color="inverse")
c3.metric("Customers served", f"{plan.served} / {n_stops}", f"{plan.served - base.served:+d} vs manual" if plan.served != base.served else None)
c4.metric("Solve time", f"{plan.solve_s:.1f} s")

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
        path = [[depot.lat, depot.lon]] + [[shown.lat[n], shown.lon[n]] for n in r.nodes] + [[depot.lat, depot.lon]]
        folium.PolyLine(path, color=c, weight=3, opacity=0.85, tooltip=f"Van {r.vehicle}: {r.distance_km:.1f} km").add_to(m)
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
        rows.append(
            f'<div class="van-row"><span class="van-dot" style="background:{color(r.vehicle)}"></span>'
            f'<span class="van-name">Van {r.vehicle}</span>'
            f'<span class="van-meta">{len(r.nodes)} stops · {r.distance_km:.1f} km · {r.load}/{settings.capacity} units · {hhmm(r.start)}–{hhmm(r.end)}</span></div>'
        )
    st.markdown("".join(rows) or "No routes.", unsafe_allow_html=True)
    st.caption(
        f"**Manual plan used for comparison:** fill one van at a time and always drive to the nearest stop that still fits, "
        f"adding a van when needed. Same rules. It needed {len(base.routes)} vans and {base.total_km:,.0f} km."
    )

# ---------------------------------------------------------------- table
table = plan_table(shown, plan)
st.subheader("Stop-by-stop plan")
st.dataframe(table.drop(columns=["lat", "lon"]), hide_index=True, width="stretch", height=320)
st.download_button("Download plan (CSV)", table.to_csv(index=False).encode(), "route_plan.csv", "text/csv")

with st.expander("How this works"):
    st.markdown(
        f"""
- **Solver:** Google OR-Tools vehicle routing with guided local search.
- **Rules it enforces:** van capacity, delivery time windows, maximum shift length, up to 2 hours of waiting at a stop, every van starts and ends at the depot.
- **Distances:** straight-line × {ROAD_FACTOR} in this demo. Client versions use real road distances and travel times (OSRM or Google Maps).
- **Easy to extend:** multiple depots, driver breaks, vehicle types, pickup & delivery, priorities, re-planning during the day.
"""
    )

st.markdown(
    f'<div class="footer">Built by {AUTHOR}. I build routing and scheduling tools for operations teams: '
    f'your rules, your data, running in days. <a href="{HIRE_URL}" target="_blank">Work with me</a></div>',
    unsafe_allow_html=True,
)

"""Road distances, drive times and route shapes from OSRM (OpenStreetMap), with a straight-line fallback.

Sample cities ship with precomputed matrices in data/*_roads.json, so the demo works even
when the public OSRM server is slow or down.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import requests

OSRM = "https://router.project-osrm.org"
HEADERS = {"User-Agent": "route-optimizer-demo (github.com/slimaneoptic)"}
DATA = Path(__file__).parent / "data"


@dataclass
class Matrices:
    dist_m: list[list[int]]
    time_s: list[list[int]]  # free-flow drive time, before any traffic factor
    source: str  # "roads" or "straight-line"


def _coords_key(coords: list[tuple[float, float]]) -> list[list[float]]:
    return [[round(lat, 5), round(lon, 5)] for lat, lon in coords]


def osrm_table(coords: list[tuple[float, float]], timeout: int = 20) -> Matrices:
    """One OSRM table request. Raises on any problem so the caller can fall back."""
    path = ";".join(f"{lon},{lat}" for lat, lon in coords)
    r = requests.get(f"{OSRM}/table/v1/driving/{path}", params={"annotations": "distance,duration"}, headers=HEADERS, timeout=timeout)
    data = r.json()
    if r.status_code != 200 or data.get("code") != "Ok":
        raise RuntimeError(f"OSRM table failed: {data.get('code')}")
    dist = [[int(v if v is not None else 10**8) for v in row] for row in data["distances"]]
    dur = [[int(v if v is not None else 10**6) for v in row] for row in data["durations"]]
    return Matrices(dist, dur, "roads")


def cached_sample(name: str, coords: list[tuple[float, float]]) -> Matrices | None:
    f = DATA / f"{name}_roads.json"
    if not f.exists():
        return None
    saved = json.loads(f.read_text())
    if saved["coords"] != _coords_key(coords):
        return None
    return Matrices(saved["distances"], saved["durations"], "roads")


def save_sample(name: str, coords: list[tuple[float, float]], m: Matrices) -> None:
    (DATA / f"{name}_roads.json").write_text(json.dumps({"coords": _coords_key(coords), "distances": m.dist_m, "durations": m.time_s}))


def route_shape(points: list[tuple[float, float]], timeout: int = 15) -> list[list[float]] | None:
    """Road-following polyline [[lat, lon], ...] through the points in order, or None."""
    if len(points) < 2:
        return None
    path = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        r = requests.get(f"{OSRM}/route/v1/driving/{path}", params={"overview": "full", "geometries": "geojson"}, headers=HEADERS, timeout=timeout)
        data = r.json()
        if data.get("code") != "Ok":
            return None
        return [[lat, lon] for lon, lat in data["routes"][0]["geometry"]["coordinates"]]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None

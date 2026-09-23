# Route optimizer

**Live demo:** https://slimaneoptic-routes.streamlit.app

Upload your delivery stops, set your fleet, and get the shortest routes that respect van capacity, delivery time windows and shift length. Each plan is compared with a quick manual plan built from the same data.

![Route optimizer on the Dubai sample](docs/screenshot.png)

On the included samples the optimized plan drives **14–36% fewer km** and often needs **one van less** than the nearest-next-stop manual plan, while serving every customer inside their window.

## What it handles

- Vehicle capacities (units, kg, pallets)
- Delivery time windows per customer (`HH:MM`)
- Service time per stop and maximum shift length
- Waiting when a van arrives early (up to 2 h)
- Stops that can't fit are flagged, not silently dropped
- Optional workload balancing between vans
- CSV in, CSV out, with a stop-by-stop plan and an interactive map

## Input format

| column           | required                 | example                  |
| ---------------- | ------------------------ | ------------------------ |
| name             | yes                      | `Customer 12`          |
| lat, lon         | yes                      | `33.5731`, `-7.5898` |
| demand           | no (default 1)           | `4`                    |
| tw_start, tw_end | no                       | `08:00`, `11:00`     |
| service_min      | no (default 5)           | `10`                   |
| depot            | no (first row otherwise) | `1` for the warehouse  |

## How it works

Google OR-Tools routing solver (guided local search) with capacity and time dimensions. In the demo, distances are straight-line × 1.3. Production versions use real road distances and travel times (OSRM or Google Maps).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
pytest -q tests   # checks capacity, time windows and shift limits on every sample
```

## Need this for your fleet?

Multiple depots, driver breaks, vehicle types, pickup & delivery, daily re-planning, and an API or Excel workflow your team already uses. I build these tools. https://www.upwork.com/freelancers/~01d6ae0f1a06a43ac0

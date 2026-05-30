"""Seed must_buy_price for all tracked routes from Perplexity market research.

These are conservative "steal" thresholds -- prices so rare that seeing one
means booking immediately regardless of season or context. Sourced from
Perplexity Pro research on 2026-05-29, treating them as informed priors to
be refined as seasonal data accumulates.

Run against the live tracker.db:
    FLI_DB_PATH=data/tracker.db uv run python scripts/set_must_buy_prices.py
"""

import os
from pathlib import Path

from fli.tracker.db import TrackerDB

# (origin, destination) -> must_buy_price in USD
MUST_BUY_PRICES: dict[tuple[str, str], float] = {
    ("BOS", "DFW"): 180,
    ("DFW", "AMS"): 525,
    ("DFW", "BCN"): 500,
    ("DFW", "BJX"): 220,
    ("DFW", "BKK"): 750,
    ("DFW", "BOG"): 350,
    ("DFW", "BOS"): 180,
    ("DFW", "CDG"): 550,
    ("DFW", "CUN"): 200,
    ("DFW", "DEN"): 120,
    ("DFW", "FCO"): 550,
    ("DFW", "FLL"): 160,
    ("DFW", "GUA"): 210,
    ("DFW", "HNL"): 450,
    ("DFW", "ICN"): 700,
    ("DFW", "JFK"): 170,
    ("DFW", "LAS"): 170,
    ("DFW", "LAX"): 170,
    ("DFW", "LHR"): 550,
    ("DFW", "LIR"): 280,
    ("DFW", "LIS"): 500,
    ("DFW", "MAD"): 500,
    ("DFW", "MCO"): 170,
    ("DFW", "MDE"): 350,
    ("DFW", "MEX"): 180,
    ("DFW", "MIA"): 160,
    ("DFW", "MID"): 220,
    ("DFW", "MTY"): 170,
    ("DFW", "NRT"): 750,
    ("DFW", "OAX"): 230,
    ("DFW", "ORD"): 140,
    ("DFW", "PNS"): 180,
    ("DFW", "PWM"): 250,
    ("DFW", "SAL"): 210,
    ("DFW", "SDQ"): 300,
    ("DFW", "SEA"): 200,
    ("DFW", "SFO"): 200,
    ("DFW", "SJO"): 260,
    ("DFW", "YVR"): 280,
    ("DFW", "YYC"): 250,
    ("PWM", "DFW"): 250,
}

db = TrackerDB()
routes = db.list_routes(active_only=False)

updated = 0
not_found = []

for route in routes:
    key = (route.origin, route.destination)
    price = MUST_BUY_PRICES.get(key)
    if price is None:
        not_found.append(f"{route.origin}->{route.destination}")
        continue
    db.update_route_must_buy_price(route.id, price)
    print(f"  {route.origin}->{route.destination}: must_buy_price = ${price:.0f}")
    updated += 1

db.close()

print(f"\nUpdated {updated} routes.")
if not_found:
    print(f"No must_buy_price defined for: {', '.join(not_found)}")

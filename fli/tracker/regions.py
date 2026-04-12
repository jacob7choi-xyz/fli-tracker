"""Route region classification using airport country metadata.

Classifies routes as 'domestic' or 'longhaul' based on the
destination country from data/airport_locations.csv.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Countries considered "domestic" or near-international
_DOMESTIC_COUNTRIES = {
    "US", "Puerto Rico",
    "Mexico", "Jamaica", "Dominican Republic",
    "Costa Rica", "Guatemala", "El Salvador",
    "Canada",
}


def _load_airport_countries() -> dict[str, str]:
    """Load airport country mapping from data/airport_locations.csv."""
    csv_path = Path(__file__).resolve().parent.parent.parent / "data" / "airport_locations.csv"
    countries: dict[str, str] = {}
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row["code"].strip()
                country = row.get("country", "").strip()
                if country:
                    countries[code] = country
    except FileNotFoundError:
        logger.warning("Airport locations file not found: %s", csv_path)
    return countries


_AIRPORT_COUNTRIES: dict[str, str] = _load_airport_countries()

RouteGroup = Literal["domestic", "longhaul"]


def route_group(origin: str, destination: str) -> RouteGroup:
    """Classify a route as domestic or longhaul based on destination country.

    Domestic includes US, Puerto Rico, Mexico, Caribbean,
    Central America, and Canada. Everything else is longhaul.
    """
    dest_country = _AIRPORT_COUNTRIES.get(destination, "")
    if dest_country in _DOMESTIC_COUNTRIES:
        return "domestic"
    return "longhaul"

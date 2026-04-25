"""Route region classification using airport country metadata.

Classifies routes as 'domestic' or 'longhaul' based on the
destination country from the bundled airport_locations.csv.
"""

from __future__ import annotations

import csv
import importlib.resources
import io
import logging
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
    """Load airport country mapping from the bundled CSV package data."""
    countries: dict[str, str] = {}
    try:
        ref = importlib.resources.files("fli.tracker.data").joinpath("airport_locations.csv")
        text = ref.read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            code = row["code"].strip()
            country = row.get("country", "").strip()
            if country:
                countries[code] = country
    except FileNotFoundError:
        logger.warning("Bundled airport_locations.csv not found in fli.tracker.data")
    return countries


_AIRPORT_COUNTRIES: dict[str, str] = _load_airport_countries()

RouteGroup = Literal["domestic", "coastal", "longhaul"]

# Major US east/west coast metros carved out into their own sweep group
# so the domestic workflow doesn't become a junk drawer as routes grow.
_COASTAL_AIRPORTS: frozenset[str] = frozenset({
    # NYC metro
    "JFK", "LGA", "EWR",
    # LA metro
    "LAX", "BUR", "SNA",
    # Other US coastal cities
    "BOS", "SEA",
})


def route_group(origin: str, destination: str) -> RouteGroup:
    """Classify a route as coastal, domestic, or longhaul.

    Coastal: major US east/west coast metros (NYC, LA, Boston, Seattle).
    Domestic: US, Puerto Rico, Mexico, Caribbean, Central America, Canada.
    Longhaul: everything else.

    Coastal is checked first so those airports don't fall through to domestic.
    """
    if destination in _COASTAL_AIRPORTS:
        return "coastal"
    dest_country = _AIRPORT_COUNTRIES.get(destination, "")
    if dest_country in _DOMESTIC_COUNTRIES:
        return "domestic"
    return "longhaul"

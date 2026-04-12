"""Scanner that sweeps tracked routes and stores price snapshots.

For each active route, builds DateSearchFilters using the same parsers
and builders that the CLI and MCP server use, calls SearchDates, and
stores the results in the tracker database.
"""

import logging
from datetime import datetime, timedelta

from fli.core.builders import build_date_search_segments
from fli.core.parsers import parse_cabin_class, parse_max_stops, resolve_airport
from fli.models import DateSearchFilters, PassengerInfo, TripType
from fli.search import SearchDates
from fli.tracker.db import TrackerDB
from fli.tracker.models import PriceSnapshot, Route

logger = logging.getLogger(__name__)


def scan_route(route: Route) -> list[PriceSnapshot]:
    """Run a date search for a single route and return price snapshots.

    Builds the search filters from the route's configuration, calls
    SearchDates, and converts the results into PriceSnapshot objects
    ready for database insertion.

    Args:
        route: The tracked route to scan. Must have an assigned id.

    Returns:
        List of PriceSnapshot objects. Empty list if no results
        or if the search fails.

    """
    if route.id is None:
        raise ValueError("Route must have an assigned id before scanning")

    origin = resolve_airport(route.origin)
    destination = resolve_airport(route.destination)
    seat_type = parse_cabin_class(route.cabin_class)
    stops = parse_max_stops(route.max_stops)

    start_date = datetime.now().date() + timedelta(days=1)
    end_date = start_date + timedelta(days=route.look_ahead)

    segments, trip_type = build_date_search_segments(
        origin=origin,
        destination=destination,
        start_date=start_date.strftime("%Y-%m-%d"),
        trip_duration=route.trip_duration if route.is_round_trip else None,
        is_round_trip=route.is_round_trip,
    )

    filters = DateSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=segments,
        stops=stops,
        seat_type=seat_type,
        from_date=start_date.strftime("%Y-%m-%d"),
        to_date=end_date.strftime("%Y-%m-%d"),
        duration=route.trip_duration if trip_type == TripType.ROUND_TRIP else None,
    )

    search_client = SearchDates()

    try:
        results = search_client.search(filters)
    except Exception:
        logger.exception("Search failed for route %s -> %s", route.origin, route.destination)
        return []

    if not results:
        logger.info("No results for route %s -> %s", route.origin, route.destination)
        return []

    snapshots = []
    for date_price in results:
        departure_date = date_price.date[0].strftime("%Y-%m-%d")
        return_date = date_price.date[1].strftime("%Y-%m-%d") if len(date_price.date) > 1 else None
        snapshots.append(
            PriceSnapshot(
                route_id=route.id,
                departure_date=departure_date,
                return_date=return_date,
                price=date_price.price,
                currency=date_price.currency or "USD",
            )
        )

    logger.info(
        "Found %d prices for route %s -> %s",
        len(snapshots),
        route.origin,
        route.destination,
    )
    return snapshots


def sweep(db: TrackerDB) -> int:
    """Scan all active routes and store price snapshots.

    Args:
        db: The tracker database to read routes from and write snapshots to.

    Returns:
        Total number of price snapshots stored across all routes.

    """
    routes = db.list_routes(active_only=True)
    if not routes:
        logger.info("No active routes to scan")
        return 0

    total = 0
    for route in routes:
        logger.info("Scanning route %d: %s -> %s", route.id, route.origin, route.destination)
        try:
            snapshots = scan_route(route)
        except Exception:
            logger.exception(
                "Failed to scan route %d: %s -> %s",
                route.id,
                route.origin,
                route.destination,
            )
            continue
        if snapshots:
            count = db.add_snapshots(snapshots)
            total += count
            logger.info("Stored %d snapshots for route %d", count, route.id)

    logger.info("Sweep complete: %d total snapshots stored", total)
    return total

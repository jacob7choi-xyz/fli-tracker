"""Notification delivery via Apprise.

Formats alert trigger messages and sends them through Apprise URLs.
Logs every sent notification to the tracker database for deduplication.
Enriches alerts with flight details (airlines, duration, stops) when possible.
"""

import logging
from datetime import datetime
from urllib.parse import quote

from fli.tracker.db import TrackerDB
from fli.tracker.detector import AlertTrigger
from fli.tracker.models import AlertType, NotificationRecord

logger = logging.getLogger(__name__)

try:
    import apprise

    _HAS_APPRISE = True
except ImportError:
    apprise = None
    _HAS_APPRISE = False


# Deal quality thresholds by region (round-trip USD)
_DEAL_THRESHOLDS: dict[str, tuple[float, float, float]] = {
    # (exceptional, great, good)
    "domestic": (100, 150, 250),
    "near_intl": (200, 300, 400),
    "europe": (500, 700, 900),
    "asia": (700, 1000, 1300),
    "other": (500, 800, 1000),
}

_REGION_MAP: dict[str, str] = {
    # Domestic US
    "LAX": "domestic", "JFK": "domestic", "MIA": "domestic", "SFO": "domestic",
    "SEA": "domestic", "ORD": "domestic", "BOS": "domestic", "LAS": "domestic",
    "HNL": "domestic", "DEN": "domestic", "FLL": "domestic", "MCO": "domestic",
    "PNS": "domestic", "ATL": "domestic", "IAH": "domestic", "PHX": "domestic",
    "SAN": "domestic", "IAD": "domestic", "DTW": "domestic", "MSP": "domestic",
    "EWR": "domestic", "CLT": "domestic", "SLC": "domestic", "PDX": "domestic",
    "TPA": "domestic", "AUS": "domestic", "BNA": "domestic", "RDU": "domestic",
    # Mexico / Central America / Caribbean / South America
    "CUN": "near_intl", "MEX": "near_intl", "SAL": "near_intl", "GUA": "near_intl",
    "SJO": "near_intl", "LIR": "near_intl", "MID": "near_intl", "MTY": "near_intl",
    "OAX": "near_intl", "BJX": "near_intl", "SDQ": "near_intl", "MDE": "near_intl",
    "BOG": "near_intl", "GDL": "near_intl", "PTY": "near_intl", "SJU": "near_intl",
    "LIM": "near_intl", "SCL": "near_intl", "GIG": "near_intl", "GRU": "near_intl",
    "EZE": "near_intl", "PUJ": "near_intl", "MBJ": "near_intl",
    # Canada
    "YVR": "near_intl", "YYC": "near_intl", "YYZ": "near_intl", "YUL": "near_intl",
    # Europe
    "LHR": "europe", "CDG": "europe", "FCO": "europe", "BCN": "europe",
    "AMS": "europe", "LIS": "europe", "MAD": "europe", "FRA": "europe",
    "MUC": "europe", "ZRH": "europe", "VIE": "europe", "CPH": "europe",
    "DUB": "europe", "ATH": "europe", "IST": "europe", "OSL": "europe",
    # Asia / Oceania
    "NRT": "asia", "ICN": "asia", "BKK": "asia", "HND": "asia",
    "HKG": "asia", "SIN": "asia", "TPE": "asia", "MNL": "asia",
    "DEL": "asia", "BOM": "asia", "SYD": "asia", "MEL": "asia",
    "AKL": "asia", "PEK": "asia", "PVG": "asia", "KUL": "asia",
}


def _get_region(destination: str) -> str:
    """Map a destination airport code to a region."""
    return _REGION_MAP.get(destination, "other")


def _rate_deal(price: float, destination: str) -> str:
    """Rate a deal based on price and destination region."""
    region = _get_region(destination)
    exceptional, great, good = _DEAL_THRESHOLDS[region]
    if price <= exceptional:
        return "INSANE DEAL"
    elif price <= great:
        return "Great deal"
    elif price <= good:
        return "Good deal"
    return "Fair price"


def _compute_nights(departure_date: str, return_date: str | None) -> int | None:
    """Compute number of nights between departure and return."""
    if not return_date:
        return None
    try:
        dep = datetime.strptime(departure_date, "%Y-%m-%d")
        ret = datetime.strptime(return_date, "%Y-%m-%d")
        return (ret - dep).days
    except ValueError:
        return None


def _build_search_url(
    origin: str, destination: str, departure: str, return_date: str | None
) -> str:
    """Build a Google Flights search URL."""
    if return_date:
        query = f"flights from {origin} to {destination} on {departure} return {return_date}"
    else:
        query = f"flights from {origin} to {destination} on {departure} one way"
    return f"https://www.google.com/travel/flights?q={quote(query)}"


def _fetch_flight_details(trigger: AlertTrigger) -> str | None:
    """Fetch the best flight details for a triggered alert.

    Runs a SearchFlights query for the specific departure date to get
    airline, duration, and stop details. Returns a formatted string
    or None if the lookup fails.
    """
    try:
        from fli.core.builders import build_flight_segments
        from fli.core.parsers import parse_cabin_class, parse_max_stops, resolve_airport
        from fli.models import FlightSearchFilters, PassengerInfo
        from fli.search import SearchFlights

        route = trigger.route
        snap = trigger.snapshot

        origin = resolve_airport(route.origin)
        destination = resolve_airport(route.destination)
        seat_type = parse_cabin_class(route.cabin_class)
        stops = parse_max_stops(route.max_stops)

        segments, trip_type = build_flight_segments(
            origin=origin,
            destination=destination,
            departure_date=snap.departure_date,
            return_date=snap.return_date,
        )

        filters = FlightSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=segments,
            stops=stops,
            seat_type=seat_type,
        )

        results = SearchFlights().search(filters)
        if not results:
            return None

        # Get the first (cheapest) result
        first = results[0]
        if isinstance(first, tuple):
            first = first[0]

        # Format duration
        hours, mins = divmod(first.duration, 60)
        duration_str = f"{hours}h {mins}m" if mins else f"{hours}h"

        # Collect unique airlines across all legs
        airlines = []
        seen = set()
        for leg in first.legs:
            name = leg.airline.name
            if name not in seen:
                airlines.append(name)
                seen.add(name)
        airline_str = ", ".join(airlines)

        # Departure and arrival times from first and last legs
        dep_time = first.legs[0].departure_datetime.strftime("%I:%M %p")
        arr_time = first.legs[-1].arrival_datetime.strftime("%I:%M %p")

        if first.stops == 0:
            stop_str = "Nonstop"
        else:
            s = "s" if first.stops > 1 else ""
            stop_str = f"{first.stops} stop{s}"

        return (
            f"Airlines: {airline_str}\n"
            f"Duration: {duration_str} ({stop_str})\n"
            f"Times: {dep_time} -> {arr_time}"
        )

    except Exception:
        logger.debug(
            "Could not fetch flight details for %s -> %s",
            trigger.route.origin,
            trigger.route.destination,
        )
        return None


def format_message(trigger: AlertTrigger) -> str:
    """Format an alert trigger into a human-readable notification message.

    Args:
        trigger: The triggered alert with route, snapshot, and price context.

    Returns:
        Formatted message string.

    """
    route = trigger.route
    snap = trigger.snapshot

    lines = [f"{route.origin} -> {route.destination}"]

    # Dates and nights
    if snap.return_date:
        nights = _compute_nights(snap.departure_date, snap.return_date)
        nights_str = f", {nights} nights" if nights else ""
        lines.append(f"Dates: {snap.departure_date} -> {snap.return_date}{nights_str}")
    else:
        lines.append(f"Departure: {snap.departure_date} (one-way)")

    # Price
    price_str = f"${snap.price:.0f} RT" if snap.return_date else f"${snap.price:.0f}"
    lines.append(f"Price: {price_str}")

    # Drop or threshold context
    if trigger.alert.alert_type == AlertType.DROP and trigger.previous_low is not None:
        drop_pct = (1 - snap.price / trigger.previous_low) * 100
        lines.append(f"Previous low: ${trigger.previous_low:.0f} RT (down {drop_pct:.1f}%)")
    elif trigger.alert.alert_type == AlertType.THRESHOLD and trigger.alert.threshold is not None:
        lines.append(f"Threshold: ${trigger.alert.threshold:.0f}")

    # Deal rating
    deal = _rate_deal(snap.price, route.destination)
    lines.append(f"Deal quality: {deal}")

    # Flight details (airlines, duration, times)
    details = _fetch_flight_details(trigger)
    if details:
        lines.append(details)
    else:
        lines.append(f"Cabin: {route.cabin_class}, Stops: {route.max_stops}")

    # Alert type
    if trigger.alert.alert_type == AlertType.DROP:
        lines.append("Alert: New all-time low")
    else:
        lines.append("Alert: Threshold hit")

    # Search link
    search_url = _build_search_url(
        route.origin, route.destination, snap.departure_date, snap.return_date
    )
    lines.append(f"\nBook now: {search_url}")

    return "\n".join(lines)


def send_notification(trigger: AlertTrigger, db: TrackerDB) -> bool:
    """Send a notification for a triggered alert and log it.

    Uses Apprise to deliver the message to the URL configured on the alert.
    Logs a NotificationRecord to the database regardless of delivery success
    for deduplication purposes (prevents retrying the same notification
    indefinitely on transient failures).

    Args:
        trigger: The triggered alert to notify about.
        db: Tracker database for logging the notification.

    Returns:
        True if the notification was delivered successfully, False otherwise.

    """
    if not _HAS_APPRISE:
        logger.error("apprise is not installed. Install it with: uv add apprise")
        return False

    message = format_message(trigger)
    title = _build_title(trigger)

    ap = apprise.Apprise()
    ap.add(trigger.alert.notify_url)

    success = ap.notify(body=message, title=title)

    # Log regardless of success to prevent re-sending on transient failures
    db.log_notification(
        NotificationRecord(
            alert_id=trigger.alert.id,
            price=trigger.snapshot.price,
            message=message,
        )
    )

    if success:
        logger.info(
            "Notification sent for %s -> %s", trigger.route.origin, trigger.route.destination
        )
    else:
        logger.error(
            "Notification delivery failed for %s -> %s via %s",
            trigger.route.origin,
            trigger.route.destination,
            trigger.alert.notify_url,
        )

    return success


def notify_all(triggers: list[AlertTrigger], db: TrackerDB) -> int:
    """Send notifications for all triggered alerts.

    Args:
        triggers: List of alert triggers to notify.
        db: Tracker database for logging notifications.

    Returns:
        Number of notifications successfully delivered.

    """
    if not triggers:
        return 0

    sent = 0
    for trigger in triggers:
        if send_notification(trigger, db):
            sent += 1

    logger.info("Sent %d/%d notifications", sent, len(triggers))
    return sent


def _build_title(trigger: AlertTrigger) -> str:
    """Build a short notification title with deal rating."""
    route = trigger.route
    deal = _rate_deal(trigger.snapshot.price, route.destination)
    if trigger.alert.alert_type == AlertType.DROP:
        return f"Price Drop ({deal}): {route.origin} -> {route.destination}"
    return f"Price Alert ({deal}): {route.origin} -> {route.destination}"

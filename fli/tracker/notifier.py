"""Notification delivery via Apprise.

Formats alert trigger messages and sends them through Apprise URLs.
Logs every sent notification to the tracker database for deduplication.
Enriches alerts with flight details (airlines, duration, stops) when possible.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from fli.models import FlightResult

from fli.tracker.db import TrackerDB
from fli.tracker.detector import AlertTrigger
from fli.tracker.models import AlertType, NotificationRecord, RouteStats

logger = logging.getLogger(__name__)

try:
    import apprise

    _HAS_APPRISE = True
except ImportError:
    apprise = None
    _HAS_APPRISE = False


# Minimum data thresholds for scoring confidence
_MIN_SNAPSHOTS = 10
_MIN_DAYS = 14
_MIN_MONTH_SAMPLES = 5
_MIN_DECILE_SAMPLES = 20

# Peak travel months (higher scores for cheap fares in expensive seasons)
_PEAK_MONTHS = {6, 7, 8, 12}

def _load_airport_locations() -> dict[str, str]:
    """Load airport city/location labels from data/airport_locations.csv.

    Returns a dict mapping IATA code to a display label like
    "Dallas-Fort Worth, TX" (domestic) or "Rome, Italy" (international).
    """
    csv_path = Path(__file__).resolve().parent.parent.parent / "data" / "airport_locations.csv"
    locations: dict[str, str] = {}
    try:
        with open(csv_path) as f:
            import csv

            reader = csv.DictReader(f)
            for row in reader:
                code = row["code"].strip()
                city = row["city"].strip()
                region = row.get("region", "").strip()
                country = row.get("country", "").strip()
                if country == "US" and region:
                    locations[code] = f"{city}, {region}"
                elif country:
                    locations[code] = f"{city}, {country}"
                else:
                    locations[code] = city
    except FileNotFoundError:
        logger.warning("Airport locations file not found: %s", csv_path)
    return locations


_AIRPORT_LOCATIONS: dict[str, str] = _load_airport_locations()


def _format_route_label(origin: str, destination: str) -> str:
    """Format a route with city names for readable digest output."""
    orig_city = _AIRPORT_LOCATIONS.get(origin, origin)
    dest_city = _AIRPORT_LOCATIONS.get(destination, destination)
    return f"{origin} ({orig_city}) -> {destination} ({dest_city})"


def _score_deal(price: float, stats: RouteStats | None, departure_month: int | None) -> str:
    """Score a deal using historical price distribution.

    Returns a label string. When insufficient data exists, returns
    "Building history..." instead of a potentially misleading rating.

    Scoring (0-100):
      Route price value (0-65):
        - Below median:       0-30 pts
        - Beat historical low: 0-20 pts
        - Near bottom decile:  0-15 pts (gated on 20+ observations)
      Seasonality (0-35):
        - Below month average: 0-25 pts (gated on 5+ month observations)
        - Peak month bonus:    0-10 pts
    """
    if stats is None:
        return "Building history..."

    if stats.total_count < _MIN_SNAPSHOTS or stats.days_of_history < _MIN_DAYS:
        return "Building history..."

    score = 0.0

    # --- Route price value (0-65) ---

    # Below median (0-30): linear scale from median to 0
    if stats.overall_median > 0:
        discount_ratio = 1 - (price / stats.overall_median)
        # Clamp: 0 if at/above median, 1.0 if free
        discount_ratio = max(0.0, min(1.0, discount_ratio))
        score += discount_ratio * 30

    # Beat historical low (0-20): 20 if at or below, partial credit near it
    if stats.all_time_min > 0:
        ratio = price / stats.all_time_min
        if ratio <= 1.0:
            score += 20
        elif ratio <= 1.1:
            # Within 10% of the low: linear from 20 down to 0
            score += 20 * (1.1 - ratio) / 0.1
        # Above 110% of low: 0 pts

    # Near bottom decile (0-15): gated on sufficient sample size
    if stats.total_count >= _MIN_DECILE_SAMPLES and stats.all_time_min > 0:
        # Approximate: how close to the min vs the median
        if stats.overall_median > stats.all_time_min:
            position = (price - stats.all_time_min) / (
                stats.overall_median - stats.all_time_min
            )
            position = max(0.0, min(1.0, position))
            score += (1 - position) * 15

    # --- Seasonality (0-35) ---

    if departure_month is not None and departure_month in stats.monthly:
        month_stats = stats.monthly[departure_month]
        if month_stats.count >= _MIN_MONTH_SAMPLES and month_stats.avg_price > 0:
            # Below month average (0-25)
            month_discount = 1 - (price / month_stats.avg_price)
            month_discount = max(0.0, min(1.0, month_discount))
            score += month_discount * 25

            # Peak month bonus (0-10): extra credit for cheap in expensive months
            if departure_month in _PEAK_MONTHS and month_discount > 0.1:
                score += min(month_discount * 15, 10)

    final_score = round(score)

    if final_score >= 80:
        return "INSANE DEAL"
    elif final_score >= 60:
        return "Great deal"
    elif final_score >= 40:
        return "Good deal"
    elif final_score >= 20:
        return "Decent"
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


# Airline perks: carry-on and checked bag policies for economy
# True = free, False = paid, None = unknown
_AIRLINE_PERKS: dict[str, dict[str, bool | None]] = {
    # US full-service
    "AA": {"carry_on": True, "checked_bag": False, "seat_selection": False},
    "DL": {"carry_on": True, "checked_bag": False, "seat_selection": False},
    "UA": {"carry_on": True, "checked_bag": False, "seat_selection": False},
    "AS": {"carry_on": True, "checked_bag": False, "seat_selection": False},
    "HA": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "B6": {"carry_on": True, "checked_bag": False, "seat_selection": False},
    # US budget
    "NK": {"carry_on": False, "checked_bag": False, "seat_selection": False},
    "F9": {"carry_on": False, "checked_bag": False, "seat_selection": False},
    "G4": {"carry_on": False, "checked_bag": False, "seat_selection": False},
    # International full-service
    "BA": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "AF": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "LH": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "KL": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "IB": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "AZ": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "TP": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "KE": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "NH": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "JL": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "OZ": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "TG": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "SQ": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "CX": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "EK": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "QR": {"carry_on": True, "checked_bag": True, "seat_selection": True},
    "TK": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "AM": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "CM": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "AV": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "AC": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    "WS": {"carry_on": True, "checked_bag": False, "seat_selection": False},
    # US hybrid / other
    "WN": {"carry_on": True, "checked_bag": True, "seat_selection": False},
    # Latin America budget / hybrid
    "VB": {"carry_on": False, "checked_bag": False, "seat_selection": False},
    "Y4": {"carry_on": False, "checked_bag": False, "seat_selection": False},
}


def _format_perks(airline_codes: list[str]) -> str | None:
    """Format airline perks summary for the primary airline."""
    if not airline_codes:
        return None
    primary = airline_codes[0]
    perks = _AIRLINE_PERKS.get(primary)
    if not perks:
        return None

    items = []
    if perks.get("carry_on") is True:
        items.append("Free carry-on")
    elif perks.get("carry_on") is False:
        items.append("Paid carry-on")
    if perks.get("checked_bag") is True:
        items.append("Free checked bag")
    elif perks.get("checked_bag") is False:
        items.append("Paid checked bag")
    if perks.get("seat_selection") is True:
        items.append("Free seat selection")

    return ", ".join(items) if items else None


def _format_flight_result(flight: FlightResult, label: str) -> list[str]:
    """Format a single FlightResult into display lines."""
    hours, mins = divmod(flight.duration, 60)
    duration_str = f"{hours}h {mins}m" if mins else f"{hours}h"

    airlines = []
    seen = set()
    for leg in flight.legs:
        if leg.airline.name not in seen:
            airlines.append(leg.airline.name)
            seen.add(leg.airline.name)
    airline_str = ", ".join(airlines)

    dep_time = flight.legs[0].departure_datetime.strftime("%I:%M %p")
    arr_time = flight.legs[-1].arrival_datetime.strftime("%I:%M %p")

    if flight.stops == 0:
        stop_str = "Nonstop"
    else:
        s = "s" if flight.stops > 1 else ""
        stop_str = f"{flight.stops} stop{s}"

    lines = [
        f"  {label}:",
        f"    Airlines: {airline_str}",
        f"    Duration: {duration_str} ({stop_str})",
        f"    Times: {dep_time} -> {arr_time}",
    ]

    perks = _format_perks(airlines)
    if perks:
        lines.append(f"    Perks: {perks}")

    return lines


def _fetch_flight_details(trigger: AlertTrigger) -> str | None:
    """Fetch flight details for a triggered alert.

    Runs a SearchFlights query for the specific departure date to get
    airline, duration, and stop details for both outbound and return.
    Returns a formatted string or None if the lookup fails.
    """
    try:
        from fli.core.builders import build_flight_segments
        from fli.core.parsers import (
            parse_cabin_class,
            parse_max_stops,
            resolve_airport,
        )
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

        first = results[0]
        lines = []

        if isinstance(first, tuple):
            # Round-trip: tuple of (outbound, return)
            outbound = first[0]
            lines.extend(_format_flight_result(outbound, "Outbound"))
            if len(first) > 1:
                ret = first[1]
                lines.extend(_format_flight_result(ret, "Return"))
        else:
            # One-way
            lines.extend(_format_flight_result(first, "Flight"))

        return "\n".join(lines)

    except Exception:
        logger.debug(
            "Could not fetch flight details for %s -> %s",
            trigger.route.origin,
            trigger.route.destination,
        )
        return None


def format_message(
    trigger: AlertTrigger,
    _stats: RouteStats | None = None,
) -> str:
    """Format an alert trigger into a human-readable notification message.

    Args:
        trigger: The triggered alert with route, snapshot, and price context.
        _stats: Optional pre-fetched route stats for deal scoring.
            When None, the deal label will show "Building history...".

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
        if trigger.previous_low > 0:
            drop_pct = (1 - snap.price / trigger.previous_low) * 100
            rt_label = " RT" if snap.return_date else ""
            lines.append(
                f"Previous low: ${trigger.previous_low:.0f}{rt_label}"
                f" (down {drop_pct:.1f}%)"
            )
        else:
            rt_label = " RT" if snap.return_date else ""
            lines.append(f"Previous low: ${trigger.previous_low:.0f}{rt_label}")
    elif trigger.alert.alert_type == AlertType.THRESHOLD and trigger.alert.threshold is not None:
        lines.append(f"Threshold: ${trigger.alert.threshold:.0f}")

    # Deal rating (data-driven scoring)
    departure_month = None
    try:
        departure_month = int(snap.departure_date.split("-")[1])
    except (IndexError, ValueError):
        pass
    deal = _score_deal(snap.price, _stats, departure_month)
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

    stats = db.get_route_stats(trigger.route.id)
    message = format_message(trigger, _stats=stats)
    title = _build_title(trigger, _stats=stats)

    ap = apprise.Apprise()
    ap.add(trigger.alert.notify_url)

    success = ap.notify(body=message, title=title)

    # Log regardless of success to prevent re-sending on transient failures
    db.log_notification(
        NotificationRecord(
            alert_id=trigger.alert.id,
            departure_date=trigger.snapshot.departure_date,
            return_date=trigger.snapshot.return_date,
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


def format_digest(triggers: list[AlertTrigger], db: TrackerDB) -> str:
    """Format multiple alert triggers into a single digest email body.

    Triggers are sorted by deal significance: drops before thresholds,
    then by price ascending. Each deal gets a compact block with route,
    dates, price, drop context, and deal rating.

    Args:
        triggers: All triggers from a single sweep.
        db: Tracker database for fetching route stats.

    Returns:
        Formatted digest string.

    """
    # Sort: drops first, then by price ascending
    def sort_key(t: AlertTrigger) -> tuple:
        type_order = 0 if t.alert.alert_type == AlertType.DROP else 1
        return (type_order, t.snapshot.price)

    sorted_triggers = sorted(triggers, key=sort_key)

    # One-line summary: count + best deal
    best = sorted_triggers[0] if sorted_triggers else None
    if best:
        dest_city = _AIRPORT_LOCATIONS.get(best.route.destination, best.route.destination)
        best_price = f"${best.snapshot.price:.0f}"
        count = len(triggers)
        summary = f"{count} deal{'s' if count != 1 else ''} - best {dest_city} {best_price}"
    else:
        summary = "No deals"

    lines = [summary, ""]

    for i, trigger in enumerate(sorted_triggers, 1):
        route = trigger.route
        snap = trigger.snapshot
        stats = db.get_route_stats(route.id)

        # Deal rating
        departure_month = None
        try:
            departure_month = int(snap.departure_date.split("-")[1])
        except (IndexError, ValueError):
            pass
        deal = _score_deal(snap.price, stats, departure_month)

        # Price line
        price_str = f"${snap.price:.0f} RT" if snap.return_date else f"${snap.price:.0f}"

        # Dates
        if snap.return_date:
            nights = _compute_nights(snap.departure_date, snap.return_date)
            nights_str = f" ({nights}n)" if nights else ""
            date_str = f"{snap.departure_date} -> {snap.return_date}{nights_str}"
        else:
            date_str = f"{snap.departure_date} (one-way)"

        # Alert type label
        if trigger.alert.alert_type == AlertType.DROP and trigger.previous_low is not None:
            if trigger.previous_low > 0:
                drop_pct = (1 - snap.price / trigger.previous_low) * 100
                alert_label = f"New low (was ${trigger.previous_low:.0f}, down {drop_pct:.1f}%)"
            else:
                alert_label = f"New low (was ${trigger.previous_low:.0f})"
        elif (
            trigger.alert.alert_type == AlertType.THRESHOLD
            and trigger.alert.threshold is not None
        ):
            alert_label = f"Below ${trigger.alert.threshold:.0f} threshold"
        else:
            alert_label = "Alert triggered"

        # Search link
        search_url = _build_search_url(
            route.origin, route.destination, snap.departure_date, snap.return_date
        )

        # Compact block
        route_label = _format_route_label(route.origin, route.destination)
        lines.append(f"{i}. {route_label}")
        lines.append(f"   {price_str}  |  {deal}")
        lines.append(f"   {date_str}")
        lines.append(f"   {alert_label}")
        lines.append(f"   Book: {search_url}")
        lines.append("")

    return "\n".join(lines)


def send_digest(triggers: list[AlertTrigger], db: TrackerDB) -> int:
    """Send a single digest email containing all triggered alerts.

    Groups triggers by notify_url and sends one digest per URL.
    Logs each trigger individually for dedup regardless of delivery success.

    Args:
        triggers: All triggers from a single sweep.
        db: Tracker database for logging and stats.

    Returns:
        Number of triggers included in successfully sent digests.

    """
    if not triggers:
        return 0

    # Filter out triggers above the route's max_price
    filtered = []
    for t in triggers:
        if t.route.max_price is not None and t.snapshot.price > t.route.max_price:
            logger.info(
                "Skipping %s -> %s ($%.0f > $%.0f max) for digest",
                t.route.origin, t.route.destination, t.snapshot.price, t.route.max_price,
            )
            continue
        filtered.append(t)

    if not filtered:
        return 0

    if not _HAS_APPRISE:
        logger.error("apprise is not installed. Install it with: uv add apprise")
        return 0

    # Group triggers by notify_url
    triggers = filtered
    by_url: dict[str, list[AlertTrigger]] = {}
    for trigger in triggers:
        url = trigger.alert.notify_url
        by_url.setdefault(url, []).append(trigger)

    total_sent = 0

    for url, url_triggers in by_url.items():
        body = format_digest(url_triggers, db)
        # Short subject for mobile: best deal at a glance
        best = min(url_triggers, key=lambda t: t.snapshot.price)
        dest_city = _AIRPORT_LOCATIONS.get(best.route.destination, best.route.destination)
        count = len(url_triggers)
        plural = "s" if count != 1 else ""
        title = f"{count} deal{plural} - best {dest_city} ${best.snapshot.price:.0f}"

        ap = apprise.Apprise()
        ap.add(url)
        success = ap.notify(body=body, title=title)

        # Log each trigger for dedup regardless of delivery success
        for trigger in url_triggers:
            db.log_notification(
                NotificationRecord(
                    alert_id=trigger.alert.id,
                    departure_date=trigger.snapshot.departure_date,
                    return_date=trigger.snapshot.return_date,
                    price=trigger.snapshot.price,
                    message=body,
                )
            )

        if success:
            total_sent += len(url_triggers)
            logger.info("Digest sent: %d alerts via %s", len(url_triggers), url)
        else:
            logger.error("Digest delivery failed via %s", url)

    return total_sent


def _build_title(
    trigger: AlertTrigger,
    _stats: RouteStats | None = None,
) -> str:
    """Build a short notification title with deal rating."""
    route = trigger.route
    departure_month = None
    try:
        departure_month = int(trigger.snapshot.departure_date.split("-")[1])
    except (IndexError, ValueError):
        pass
    deal = _score_deal(trigger.snapshot.price, _stats, departure_month)
    if trigger.alert.alert_type == AlertType.DROP:
        return f"Price Drop ({deal}): {route.origin} -> {route.destination}"
    return f"Price Alert ({deal}): {route.origin} -> {route.destination}"

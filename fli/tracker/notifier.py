"""Notification delivery via Apprise.

Formats alert trigger messages and sends them through Apprise URLs.
Logs every sent notification to the tracker database for deduplication.
Enriches alerts with flight details (airlines, duration, stops) when possible.
"""

from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from fli.models import FlightResult

from fli.tracker.db import TrackerDB
from fli.tracker.detector import AlertTrigger
from fli.tracker.models import AlertType, NotificationRecord, RouteStats

logger = logging.getLogger(__name__)

# Constant markers stored in notification_log.message. The column may never
# carry rendered content: dedup reads only (alert_id, departure_date,
# return_date, price), and persisting bodies caused the 2026-07 DB bloat
# incident. Historical rows sanitized during remediation carry "legacy".
MESSAGE_SINGLE = "single"
MESSAGE_DIGEST = "digest"


class NotificationError(RuntimeError):
    """Base for every way the notification capability can fail.

    All notification failures normalize to this hierarchy so callers have
    ONE containment path: collection and archive survive, the run goes red.
    Distinct failure modes must never diverge into different behaviors
    (green-on-delivery-failure was a 2026-07 incident review finding).
    """


class NotificationConfigError(NotificationError):
    """Raised when notification delivery is requested but NOTIFY_URL is not set.

    Fail-closed by design: there is no fallback to database-stored URLs.
    Credentials are resolved from the environment at send time only.
    """


class NotificationDeliveryError(NotificationError):
    """Raised when Apprise reports the notification was not delivered.

    No dedup rows are written for failed deliveries: a suppression record
    for a message the user never received would permanently silence that
    fare event. Bias: false duplicate over false suppression -- a delivery
    falsely reported as failed costs one repeat email next sweep; a
    suppression row on a real failure costs a deal never seen.
    """


class NotificationDependencyError(NotificationError):
    """Raised when the apprise package is not importable.

    A missing dependency is a lost capability, not a skippable feature:
    treating it as a log-line-and-continue made it a silent failure mode.
    """


def _resolve_notify_url() -> str:
    """Return the notification target from the NOTIFY_URL environment variable.

    Raises:
        NotificationConfigError: If NOTIFY_URL is unset or empty.

    """
    url = os.environ.get("NOTIFY_URL")
    if not url:
        raise NotificationConfigError("NOTIFY_URL is not configured")
    return url


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

# Peak travel months (higher scores for cheap fares in expensive seasons)
_PEAK_MONTHS = {6, 7, 8, 12}


def _lead_time_bucket(lead_days: int) -> str | None:
    """Map days-before-departure to a lead-time bucket label."""
    if lead_days < 0:
        return None
    if lead_days <= 7:
        return "0-7"
    if lead_days <= 14:
        return "8-14"
    if lead_days <= 30:
        return "15-30"
    if lead_days <= 60:
        return "31-60"
    return None


def _compute_percentile_rank(price: float, percentiles: dict[int, float]) -> float:
    """Estimate where price falls in the route's empirical distribution (0-100).

    0 = at or below the observed minimum, 100 = at or above the observed maximum.
    Uses linear interpolation between known breakpoints (p10, p25, p50, p75, p90).
    """
    if not percentiles:
        return 50.0

    breakpoints = sorted(percentiles.items())
    pct_lo, val_lo = breakpoints[0]

    if price <= val_lo:
        return max(0.0, pct_lo * price / val_lo) if val_lo > 0 else 0.0

    pct_hi, val_hi = breakpoints[-1]
    if price >= val_hi:
        return 100.0

    for i in range(len(breakpoints) - 1):
        pct_lo, val_lo = breakpoints[i]
        pct_hi, val_hi = breakpoints[i + 1]
        if val_lo <= price <= val_hi:
            if val_hi == val_lo:
                return float(pct_lo)
            frac = (price - val_lo) / (val_hi - val_lo)
            return pct_lo + frac * (pct_hi - pct_lo)

    return 50.0


def _load_airport_locations() -> dict[str, str]:
    """Load airport city/location labels from the bundled CSV package data.

    Returns a dict mapping IATA code to a display label like
    "Dallas-Fort Worth, TX" (domestic) or "Rome, Italy" (international).
    """
    import csv
    import importlib.resources
    import io

    locations: dict[str, str] = {}
    try:
        ref = importlib.resources.files("fli.tracker.data").joinpath("airport_locations.csv")
        text = ref.read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(text))
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
        logger.warning("Bundled airport_locations.csv not found in fli.tracker.data")
    return locations


_AIRPORT_LOCATIONS: dict[str, str] = _load_airport_locations()

# Re-export country data from regions module
from fli.tracker.regions import _AIRPORT_COUNTRIES  # noqa: E402

# US/Puerto Rico only -- for email digest section headers
_US_COUNTRIES = {"US", "Puerto Rico"}


def _is_domestic_route(origin: str, destination: str) -> bool:
    """Check if both airports are in the US or Puerto Rico.

    This is for email digest sectioning only. The workflow route
    grouping in regions.py uses a broader definition that includes
    Mexico, Caribbean, Central America, and Canada.
    """
    orig_country = _AIRPORT_COUNTRIES.get(origin, "")
    dest_country = _AIRPORT_COUNTRIES.get(destination, "")
    return orig_country in _US_COUNTRIES and dest_country in _US_COUNTRIES


def _format_route_label(origin: str, destination: str) -> str:
    """Format a route with city names for readable digest output."""
    orig_city = _AIRPORT_LOCATIONS.get(origin, origin)
    dest_city = _AIRPORT_LOCATIONS.get(destination, destination)
    return f"{origin} ({orig_city}) -> {destination} ({dest_city})"


def _score_deal(price: float, stats: RouteStats | None, departure_date: str | None) -> str:
    """Score a deal using the route's historical price distribution.

    Composite 0-100 score with four components:
      Z-score (0-40):        how many std devs below the route mean
      Percentile rank (0-30): position in the empirical distribution
      Lead-time (0-20):      vs. typical price for this booking window
      Seasonality (0-10):    cheap fare during peak travel months

    Returns a label string. Logs score breakdown at DEBUG level.
    When insufficient data exists, returns "Building history...".
    """
    if stats is None:
        return "Building history..."
    if stats.total_count < _MIN_SNAPSHOTS or stats.days_of_history < _MIN_DAYS:
        return "Building history..."

    score = 0.0
    components: dict[str, object] = {}

    # --- Z-score component (0-40) ---
    if stats.price_stddev and stats.price_stddev > 0:
        z = (price - stats.price_mean) / stats.price_stddev
        z_pts = max(0.0, min(40.0, -z * 20.0))
        score += z_pts
        components["z"] = round(z, 2)
        components["z_pts"] = round(z_pts, 1)

    # --- Percentile rank component (0-30) ---
    if stats.price_percentiles:
        pct_rank = _compute_percentile_rank(price, stats.price_percentiles)
        pct_pts = max(0.0, (50.0 - pct_rank) / 50.0 * 30.0)
        score += pct_pts
        components["pct_rank"] = round(pct_rank, 1)
        components["pct_pts"] = round(pct_pts, 1)

    # --- Lead-time component (0-20) ---
    if departure_date and stats.lead_time_buckets:
        try:
            from datetime import date as _date

            dep = _date.fromisoformat(departure_date)
            lead_days = (dep - _date.today()).days
            bucket = _lead_time_bucket(lead_days)
            bucket_avg = stats.lead_time_buckets.get(bucket) if bucket else None
            if bucket_avg and bucket_avg > 0:
                lt_discount = (bucket_avg - price) / bucket_avg
                lt_pts = max(0.0, min(20.0, lt_discount * 40.0))
                score += lt_pts
                components["lt_bucket"] = bucket
                components["lt_discount_pct"] = round(lt_discount * 100, 1)
                components["lt_pts"] = round(lt_pts, 1)
        except ValueError:
            pass

    # --- Seasonality bonus (0-10) ---
    dep_month = None
    if departure_date:
        try:
            dep_month = int(departure_date.split("-")[1])
        except (IndexError, ValueError):
            pass
    if dep_month is not None and dep_month in _PEAK_MONTHS and dep_month in stats.monthly:
        month_stats = stats.monthly[dep_month]
        if month_stats.count >= _MIN_MONTH_SAMPLES and month_stats.avg_price > 0:
            month_discount = (month_stats.avg_price - price) / month_stats.avg_price
            if month_discount > 0.05:
                peak_pts = min(10.0, month_discount * 20.0)
                score += peak_pts
                components["peak_pts"] = round(peak_pts, 1)

    final_score = round(score)
    components["total"] = final_score
    logger.debug("Deal score $%.0f (dep=%s): %s", price, departure_date, components)

    if final_score >= 80:
        return "BUY NOW"
    if final_score >= 60:
        return "Strong buy"
    if final_score >= 40:
        return "Worth watching"
    if final_score >= 20:
        return "Meh"
    return "Skip"


def _build_trend_line(
    price: float, stats: RouteStats | None, departure_date: str | None
) -> str | None:
    """Build a compact trend summary, e.g. '38% below median; bottom 18% of prices'.

    Returns None when there is insufficient data to make a meaningful claim.
    Parts: median context, all-time-low, percentile rank, lead-time, monthly bonus.
    """
    if stats is None:
        return None
    if stats.total_count < _MIN_SNAPSHOTS or stats.days_of_history < _MIN_DAYS:
        return None

    dep_month = None
    if departure_date:
        try:
            dep_month = int(departure_date.split("-")[1])
        except (IndexError, ValueError):
            pass

    parts = []

    # 1. vs. overall median
    if stats.overall_median > 0:
        pct = (stats.overall_median - price) / stats.overall_median * 100
        if pct >= 1:
            parts.append(f"{pct:.0f}% below median")
        elif pct <= -1:
            parts.append(f"{abs(pct):.0f}% above median")
        else:
            parts.append("at median")

    # 2. vs. all-time low
    if stats.all_time_min > 0:
        ratio = price / stats.all_time_min
        if ratio <= 1.0:
            parts.append(f"new {stats.days_of_history}d low")
        elif ratio <= 1.05:
            parts.append(f"near all-time low (${stats.all_time_min:.0f})")
        else:
            above = price - stats.all_time_min
            parts.append(f"${above:.0f} above all-time low")

    # 3. Percentile rank (only if in bottom quartile -- adds meaningful context)
    if stats.price_percentiles:
        pct_rank = _compute_percentile_rank(price, stats.price_percentiles)
        if pct_rank <= 25:
            parts.append(f"bottom {pct_rank:.0f}% of prices")

    # 4. Lead-time context (only if >= 10% below bucket avg)
    if departure_date and stats.lead_time_buckets:
        try:
            from datetime import date as _date

            dep = _date.fromisoformat(departure_date)
            lead_days = (dep - _date.today()).days
            bucket = _lead_time_bucket(lead_days)
            bucket_avg = stats.lead_time_buckets.get(bucket) if bucket else None
            if bucket_avg and bucket_avg > 0:
                lt_pct = (bucket_avg - price) / bucket_avg * 100
                if lt_pct >= 10:
                    parts.append(f"{lt_pct:.0f}% below {bucket}d avg")
        except ValueError:
            pass

    # 5. vs. monthly avg (bonus only when notably below: >=10% discount, >=5 samples)
    if dep_month is not None and dep_month in stats.monthly:
        month_stats = stats.monthly[dep_month]
        if month_stats.count >= _MIN_MONTH_SAMPLES and month_stats.avg_price > 0:
            month_pct = (month_stats.avg_price - price) / month_stats.avg_price * 100
            if month_pct >= 10:
                month_name = datetime(2000, dep_month, 1).strftime("%b")
                parts.append(f"{month_pct:.0f}% below {month_name} avg")

    return "; ".join(parts) if parts else None


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


@dataclass
class LegDetail:
    """Structured flight leg data for rendering."""

    label: str
    airlines: str
    duration: str
    stops: str
    dep_time: str
    arr_time: str
    perks: str | None


def _extract_leg_detail(flight: FlightResult, label: str) -> LegDetail:
    """Extract structured data from a FlightResult."""
    hours, mins = divmod(flight.duration, 60)
    duration_str = f"{hours}h {mins}m" if mins else f"{hours}h"

    airline_names = []
    airline_codes = []
    seen: set[str] = set()
    for leg in flight.legs:
        if leg.airline.name not in seen:
            airline_names.append(leg.airline.value)
            airline_codes.append(leg.airline.name)
            seen.add(leg.airline.name)

    if flight.stops == 0:
        stop_str = "Nonstop"
    else:
        s = "s" if flight.stops > 1 else ""
        stop_str = f"{flight.stops} stop{s}"

    return LegDetail(
        label=label,
        airlines=", ".join(airline_names),
        duration=duration_str,
        stops=stop_str,
        dep_time=flight.legs[0].departure_datetime.strftime("%I:%M %p"),
        arr_time=flight.legs[-1].arrival_datetime.strftime("%I:%M %p"),
        perks=_format_perks(airline_codes),
    )


def fetch_flight_details(trigger: AlertTrigger) -> list[LegDetail]:
    """Fetch structured flight details for a triggered alert.

    Runs a SearchFlights query for the specific departure date.
    Returns a list of LegDetail (outbound + optional return),
    or an empty list if the lookup fails.
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
            return []

        first = results[0]
        legs: list[LegDetail] = []

        if isinstance(first, tuple):
            legs.append(_extract_leg_detail(first[0], "Outbound"))
            if len(first) > 1:
                legs.append(_extract_leg_detail(first[1], "Return"))
        else:
            legs.append(_extract_leg_detail(first, "Flight"))

        return legs

    except Exception:
        logger.debug(
            "Could not fetch flight details for %s -> %s",
            trigger.route.origin,
            trigger.route.destination,
        )
        return []


def _render_legs_text(legs: list[LegDetail]) -> str:
    """Render flight leg details as plain text."""
    lines = []
    for leg in legs:
        lines.append(f"  {leg.label}:")
        lines.append(f"    Airlines: {leg.airlines}")
        lines.append(f"    Duration: {leg.duration} ({leg.stops})")
        lines.append(f"    Times: {leg.dep_time} -> {leg.arr_time}")
        if leg.perks:
            lines.append(f"    Perks: {leg.perks}")
    return "\n".join(lines)


def _render_legs_html(legs: list[LegDetail]) -> str:
    """Render flight leg details as HTML for digest emails."""
    parts = []
    for leg in legs:
        perks_html = (
            f'<div style="font-size:12px;color:#888;margin-top:2px;">{html.escape(leg.perks)}</div>'
            if leg.perks
            else ""
        )
        parts.append(
            f'<div style="margin-top:6px;font-size:13px;">'
            f"<strong>{html.escape(leg.label)}:</strong> {html.escape(leg.airlines)}<br>"
            f"{html.escape(leg.duration)} ({html.escape(leg.stops)}) &middot; "
            f"{html.escape(leg.dep_time)} - {html.escape(leg.arr_time)}"
            f"{perks_html}</div>"
        )
    return "".join(parts)


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
                f"Previous low: ${trigger.previous_low:.0f}{rt_label} (down {drop_pct:.1f}%)"
            )
        else:
            rt_label = " RT" if snap.return_date else ""
            lines.append(f"Previous low: ${trigger.previous_low:.0f}{rt_label}")
    elif trigger.alert.alert_type == AlertType.THRESHOLD and trigger.alert.threshold is not None:
        lines.append(f"Threshold: ${trigger.alert.threshold:.0f}")

    # Deal rating (data-driven scoring)
    deal = _score_deal(snap.price, _stats, snap.departure_date)
    lines.append(f"Deal quality: {deal}")
    trend = _build_trend_line(snap.price, _stats, snap.departure_date)
    if trend:
        lines.append(f"Trend: {trend}")

    # Flight details (airlines, duration, times)
    legs = fetch_flight_details(trigger)
    if legs:
        lines.append(_render_legs_text(legs))
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
    """Send a notification for a triggered alert and log it on delivery.

    Dedup rows are written ONLY after successful delivery: a suppression
    record must mean the user was actually told. A failed delivery leaves
    the fare event retry-eligible on the next sweep (bias: false duplicate
    over false suppression).

    Args:
        trigger: The triggered alert to notify about.
        db: Tracker database for logging the notification.

    Returns:
        True on successful delivery.

    Raises:
        NotificationDependencyError: If apprise is not installed.
        NotificationConfigError: If NOTIFY_URL is not set.
        NotificationDeliveryError: If Apprise reports delivery failure.

    """
    if not _HAS_APPRISE:
        raise NotificationDependencyError(
            "apprise is not installed. Install it with: uv sync --extra tracker"
        )

    # Resolve before any work or dedup logging: a config error must not
    # suppress future delivery of this fare event once config is fixed.
    notify_url = _resolve_notify_url()

    stats = db.get_route_stats(trigger.route.id)
    message = format_message(trigger, _stats=stats)
    title = _build_title(trigger, _stats=stats)

    ap = apprise.Apprise()
    ap.add(notify_url)

    success = ap.notify(body=message, title=title)
    if not success:
        logger.error(
            "Notification delivery failed for %s -> %s",
            trigger.route.origin,
            trigger.route.destination,
        )
        raise NotificationDeliveryError("notification delivery failed")

    # Delivered: now it is true that the notification was sent. message is
    # a constant marker: dedup reads only the structural columns, and
    # storing rendered bodies caused the 2026-07 DB bloat incident.
    db.log_notification(
        NotificationRecord(
            alert_id=trigger.alert.id,
            departure_date=trigger.snapshot.departure_date,
            return_date=trigger.snapshot.return_date,
            price=trigger.snapshot.price,
            message=MESSAGE_SINGLE,
        )
    )
    logger.info("Notification sent for %s -> %s", trigger.route.origin, trigger.route.destination)
    return True


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
        try:
            if send_notification(trigger, db):
                sent += 1
        except NotificationError:
            logger.error(
                "Notification failed for %s -> %s; continuing with remaining triggers",
                trigger.route.origin,
                trigger.route.destination,
            )

    logger.info("Sent %d/%d notifications", sent, len(triggers))
    return sent


def _render_trigger_block(
    trigger: AlertTrigger,
    index: int,
    stats: RouteStats | None,
    fetch_details: bool = False,
) -> str:
    """Render a single trigger as an HTML card block for the digest."""
    route = trigger.route
    snap = trigger.snapshot

    # Must-buy check: bypass scorer if price is below the must_buy threshold
    is_must_buy = route.must_buy_price is not None and snap.price <= route.must_buy_price
    if is_must_buy:
        deal = "MUST BUY"
        trend = None
    else:
        deal = _score_deal(snap.price, stats, snap.departure_date)
        trend = _build_trend_line(snap.price, stats, snap.departure_date)
    trend_html = (
        f'<div style="font-size:12px;color:#888;margin:2px 0;">{html.escape(trend)}</div>'
        if trend
        else ""
    )

    # Price
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
    elif trigger.alert.alert_type == AlertType.THRESHOLD and trigger.alert.threshold is not None:
        alert_label = f"Below ${trigger.alert.threshold:.0f} threshold"
    else:
        alert_label = "Alert triggered"

    # Search link
    search_url = _build_search_url(
        route.origin, route.destination, snap.departure_date, snap.return_date
    )

    # Route label with city names (escaped for HTML)
    orig_city = html.escape(_AIRPORT_LOCATIONS.get(route.origin, route.origin))
    dest_city_label = html.escape(_AIRPORT_LOCATIONS.get(route.destination, route.destination))

    # Fetch real flight details (airline, duration, stops) -- only for top alerts
    # to avoid making dozens of live API calls per digest sweep
    legs = fetch_flight_details(trigger) if fetch_details else []
    flight_html = _render_legs_html(legs) if legs else ""

    border_color = "#d93025" if is_must_buy else "#4a90d9"
    bg_color = "#fff8f7" if is_must_buy else "#f8f9fa"
    must_buy_banner = (
        '<div style="font-size:11px;font-weight:bold;color:#d93025;'
        'letter-spacing:0.05em;margin-bottom:4px;">MUST BUY -- below steal threshold</div>'
        if is_must_buy
        else ""
    )

    return (
        f'<div style="margin-bottom:20px;padding:12px;'
        f'border-left:3px solid {border_color};background:{bg_color};">'
        f"{must_buy_banner}"
        f'<div style="font-size:16px;font-weight:bold;">'
        f"{index}. {route.origin} -> {route.destination}</div>"
        f'<div style="font-size:13px;color:#666;">'
        f"{orig_city} -> {dest_city_label}</div>"
        f'<div style="font-size:20px;font-weight:bold;margin:8px 0;">'
        f"{price_str} &middot; {deal}</div>"
        f"{trend_html}"
        f'<div style="font-size:14px;">{date_str}</div>'
        f'<div style="font-size:13px;color:#666;margin:4px 0;">'
        f"{html.escape(alert_label)}</div>"
        f"{flight_html}"
        f'<div style="margin-top:8px;">'
        f'<a href="{search_url}" style="color:#1a73e8;">'
        f"Book on Google Flights</a></div>"
        f"</div>"
    )


def format_digest(triggers: list[AlertTrigger], db: TrackerDB) -> str:
    """Format multiple alert triggers into a single digest email body.

    Triggers are split into Domestic and International sections.
    Within each section, drops appear before thresholds, then sorted
    by price ascending.

    Args:
        triggers: All triggers from a single sweep.
        db: Tracker database for fetching route stats.

    Returns:
        Formatted HTML digest string.

    """

    # Sort: drops first, then by price ascending
    def sort_key(t: AlertTrigger) -> tuple:
        type_order = 0 if t.alert.alert_type == AlertType.DROP else 1
        return (type_order, t.snapshot.price)

    sorted_triggers = sorted(triggers, key=sort_key)

    # Pre-fetch stats for all unique routes -- avoids N+1 (one get_route_stats per trigger card)
    stats_cache: dict[int, RouteStats | None] = {
        rid: db.get_route_stats(rid) for rid in {t.route.id for t in sorted_triggers}
    }

    # One-line summary: count + best deal
    best = sorted_triggers[0] if sorted_triggers else None
    if best:
        dest_city = html.escape(
            _AIRPORT_LOCATIONS.get(best.route.destination, best.route.destination)
        )
        best_price = f"${best.snapshot.price:.0f}"
        count = len(triggers)
        plural = "s" if count != 1 else ""
        summary = f"{count} deal{plural} - best {dest_city} {best_price}"
    else:
        summary = "No deals"

    # Split into domestic and international
    domestic = [
        t for t in sorted_triggers if _is_domestic_route(t.route.origin, t.route.destination)
    ]
    international = [
        t for t in sorted_triggers if not _is_domestic_route(t.route.origin, t.route.destination)
    ]

    # Render sections with continuous numbering.
    # Live flight detail fetches (airline/duration/stops) are capped at 5 per digest
    # to prevent the sweep from timing out when many alerts fire simultaneously.
    sections = []
    counter = 1
    detail_budget = 5

    if domestic:
        blocks = []
        for trigger in domestic:
            blocks.append(
                _render_trigger_block(
                    trigger,
                    counter,
                    stats_cache.get(trigger.route.id),
                    fetch_details=detail_budget > 0,
                )
            )
            detail_budget = max(0, detail_budget - 1)
            counter += 1
        sections.append(
            f'<h3 style="margin:16px 0 8px;color:#333;">Domestic Deals</h3>{"".join(blocks)}'
        )

    if international:
        blocks = []
        for trigger in international:
            blocks.append(
                _render_trigger_block(
                    trigger,
                    counter,
                    stats_cache.get(trigger.route.id),
                    fetch_details=detail_budget > 0,
                )
            )
            detail_budget = max(0, detail_budget - 1)
            counter += 1
        sections.append(
            f'<h3 style="margin:16px 0 8px;color:#333;">International Deals</h3>{"".join(blocks)}'
        )

    body = (
        f'<div style="font-family:sans-serif;max-width:600px;margin:0 auto;">'
        f'<h2 style="margin-bottom:16px;">{html.escape(summary)}</h2>'
        f"{''.join(sections)}"
        f"</div>"
    )
    return body


def send_digest(triggers: list[AlertTrigger], db: TrackerDB) -> int:
    """Send a single digest email containing all triggered alerts.

    The target comes from the NOTIFY_URL environment variable. Dedup rows
    are written ONLY after successful delivery, so failed digests leave
    every fare event retry-eligible on the next sweep.

    Args:
        triggers: All triggers from a single sweep.
        db: Tracker database for logging and stats.

    Returns:
        Number of triggers in the delivered digest (0 if none qualified).

    Raises:
        NotificationDependencyError: If apprise is not installed.
        NotificationConfigError: If NOTIFY_URL is not set.
        NotificationDeliveryError: If Apprise reports delivery failure.

    """
    if not triggers:
        return 0

    # Filter out triggers above the route's max_price, unless they hit must_buy_price
    filtered = []
    for t in triggers:
        is_must_buy = (
            t.route.must_buy_price is not None and t.snapshot.price <= t.route.must_buy_price
        )
        if (
            not is_must_buy
            and t.route.max_price is not None
            and t.snapshot.price > t.route.max_price
        ):  # noqa: E501
            logger.info(
                "Skipping %s -> %s ($%.0f > $%.0f max) for digest",
                t.route.origin,
                t.route.destination,
                t.snapshot.price,
                t.route.max_price,
            )
            continue
        filtered.append(t)

    if not filtered:
        return 0

    if not _HAS_APPRISE:
        raise NotificationDependencyError(
            "apprise is not installed. Install it with: uv sync --extra tracker"
        )

    # Resolve before any work or dedup logging: a config error must not
    # suppress future delivery of these fare events once config is fixed.
    notify_url = _resolve_notify_url()

    triggers = filtered
    body = format_digest(triggers, db)
    # Short subject for mobile: best deal at a glance
    best = min(triggers, key=lambda t: t.snapshot.price)
    dest_city = _AIRPORT_LOCATIONS.get(best.route.destination, best.route.destination)
    count = len(triggers)
    plural = "s" if count != 1 else ""
    title = f"{count} deal{plural} - best {dest_city} ${best.snapshot.price:.0f}"

    ap = apprise.Apprise()
    ap.add(notify_url)
    success = ap.notify(body=body, title=title, body_format="html")
    if not success:
        # No dedup rows: a suppression record for a digest the user never
        # received would permanently silence these fare events. They stay
        # retry-eligible next sweep (false duplicate over false suppression).
        logger.error("Digest delivery failed")
        raise NotificationDeliveryError("digest delivery failed")

    # Delivered: log each trigger for dedup. message is a constant marker:
    # dedup reads only the structural columns, and storing the rendered
    # digest HTML once per trigger is what bloated tracker.db past GitHub's
    # 100 MiB push limit (2026-07 incident).
    for trigger in triggers:
        db.log_notification(
            NotificationRecord(
                alert_id=trigger.alert.id,
                departure_date=trigger.snapshot.departure_date,
                return_date=trigger.snapshot.return_date,
                price=trigger.snapshot.price,
                message=MESSAGE_DIGEST,
            )
        )

    logger.info("Digest sent: %d alerts", count)
    return count


def _build_title(
    trigger: AlertTrigger,
    _stats: RouteStats | None = None,
) -> str:
    """Build a short notification title with deal rating."""
    route = trigger.route
    snap = trigger.snapshot
    if route.must_buy_price is not None and snap.price <= route.must_buy_price:
        return f"MUST BUY: {route.origin} -> {route.destination} ${snap.price:.0f}"
    deal = _score_deal(snap.price, _stats, snap.departure_date)
    if trigger.alert.alert_type == AlertType.DROP:
        return f"Price Drop ({deal}): {route.origin} -> {route.destination}"
    return f"Price Alert ({deal}): {route.origin} -> {route.destination}"

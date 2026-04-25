"""Price drop detection for tracked routes.

Compares new price snapshots against historical data and active alerts.
Produces trigger objects that the notifier can act on.
"""

import logging

from fli.tracker.db import TrackerDB
from fli.tracker.models import Alert, AlertType, PriceSnapshot, Route

logger = logging.getLogger(__name__)

# Flights under this price re-alert on any further drop (even $1).
_CHEAP_FLIGHT_THRESHOLD = 55.0


def _min_improvement(last_notified: float) -> float:
    """Return the minimum price drop required to re-alert, scaled by price band.

    Uses a hybrid max(flat_floor, percent) rule so the threshold feels
    meaningful across both cheap domestic and expensive international fares.

    Band       Flat floor  Percent  Example threshold
    $55-$199   $20         10%      $20 at $55, $19.9 -> uses $20
    $200-$599  $30         8%       $36 at $450
    $600+      $50         6%       $60 at $1000
    """
    if last_notified < 200:
        return max(20.0, last_notified * 0.10)
    elif last_notified < 600:
        return max(30.0, last_notified * 0.08)
    else:
        return max(50.0, last_notified * 0.06)


class AlertTrigger:
    """Represents a triggered alert with context for notification formatting.

    This is a plain data container, not a Pydantic model, because it is
    never persisted or serialized. It exists only to pass data from the
    detector to the notifier within a single sweep.
    """

    def __init__(
        self,
        alert: Alert,
        route: Route,
        snapshot: PriceSnapshot,
        previous_low: float | None = None,
    ):
        """Initialize with alert context for notification formatting."""
        self.alert = alert
        self.route = route
        self.snapshot = snapshot
        self.previous_low = previous_low


def check_alerts(
    db: TrackerDB,
    route: Route,
    snapshots: list[PriceSnapshot],
) -> list[AlertTrigger]:
    """Check new snapshots against active alerts for a route.

    For each snapshot, checks two kinds of alerts:
    - "drop": fires if the snapshot's price is a new all-time low
      for that route and departure date.
    - "threshold": fires if the snapshot's price is at or below the
      alert's threshold, and a notification for that exact (alert,
      departure_date, price) combination has not already been sent.

    Args:
        db: Tracker database for historical price lookups and dedup checks.
        route: The route these snapshots belong to.
        snapshots: Newly scanned price snapshots to evaluate.

    Returns:
        List of AlertTrigger objects for alerts that should fire.

    """
    alerts = db.list_alerts(route_id=route.id, active_only=True)
    if not alerts:
        return []

    triggers = []

    for snapshot in snapshots:
        previous_low = db.get_min_price(route.id, snapshot.departure_date)

        for alert in alerts:
            trigger = _evaluate_alert(db, alert, route, snapshot, previous_low)
            if trigger is not None:
                triggers.append(trigger)

    return triggers


def _evaluate_alert(
    db: TrackerDB,
    alert: Alert,
    route: Route,
    snapshot: PriceSnapshot,
    previous_low: float | None,
) -> AlertTrigger | None:
    """Evaluate a single alert against a single snapshot.

    Returns an AlertTrigger if the alert should fire, None otherwise.
    """
    if alert.alert_type == AlertType.DROP:
        return _check_drop(db, alert, route, snapshot, previous_low)
    elif alert.alert_type == AlertType.THRESHOLD:
        return _check_threshold(db, alert, route, snapshot)
    return None


def _check_drop(
    db: TrackerDB,
    alert: Alert,
    route: Route,
    snapshot: PriceSnapshot,
    previous_low: float | None,
) -> AlertTrigger | None:
    """Check if the snapshot represents a new all-time low.

    A drop alert fires when the new price is strictly less than the
    historical minimum. If there is no history (first scan), it does
    not fire, because there is no drop to report.

    Re-alert suppression: for flights at or above $55, only the first
    notification fires. For flights under $55, any further price drop
    (even $1) triggers a new alert.
    """
    if previous_low is None:
        return None

    if snapshot.price >= previous_low:
        return None

    last_notified = db.get_last_notified_price(
        alert.id, snapshot.departure_date, snapshot.return_date
    )
    if last_notified is not None:
        if snapshot.price < _CHEAP_FLIGHT_THRESHOLD:
            # Sub-$55 flight: re-alert on any further drop, even $1.
            if snapshot.price >= last_notified:
                return None
        else:
            # $55+ flight: only re-alert if improvement meets the price-scaled threshold.
            if (last_notified - snapshot.price) < _min_improvement(last_notified):
                return None

    logger.info(
        "Drop detected: %s -> %s on %s, $%.2f (was $%.2f)",
        route.origin,
        route.destination,
        snapshot.departure_date,
        snapshot.price,
        previous_low,
    )
    return AlertTrigger(
        alert=alert,
        route=route,
        snapshot=snapshot,
        previous_low=previous_low,
    )


def _check_threshold(
    db: TrackerDB,
    alert: Alert,
    route: Route,
    snapshot: PriceSnapshot,
) -> AlertTrigger | None:
    """Check if the snapshot price meets or is below the alert threshold.

    Deduplicates by checking if a notification with the same (alert_id,
    departure_date, price) has already been sent.
    """
    if alert.threshold is None:
        return None

    if snapshot.price > alert.threshold:
        return None

    last_notified = db.get_last_notified_price(
        alert.id, snapshot.departure_date, snapshot.return_date
    )
    if last_notified is not None:
        if snapshot.price < _CHEAP_FLIGHT_THRESHOLD:
            # Sub-$55 flight: re-alert on any further drop, even $1.
            if snapshot.price >= last_notified:
                return None
        else:
            # $55+ flight: only re-alert if improvement meets the price-scaled threshold.
            if (last_notified - snapshot.price) < _min_improvement(last_notified):
                return None

    logger.info(
        "Threshold met: %s -> %s on %s, $%.2f (threshold $%.2f)",
        route.origin,
        route.destination,
        snapshot.departure_date,
        snapshot.price,
        alert.threshold,
    )
    return AlertTrigger(
        alert=alert,
        route=route,
        snapshot=snapshot,
    )

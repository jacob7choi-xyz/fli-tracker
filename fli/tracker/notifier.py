"""Notification delivery via Apprise.

Formats alert trigger messages and sends them through Apprise URLs.
Logs every sent notification to the tracker database for deduplication.
"""

import logging

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


def format_message(trigger: AlertTrigger) -> str:
    """Format an alert trigger into a human-readable notification message.

    Args:
        trigger: The triggered alert with route, snapshot, and price context.

    Returns:
        Formatted message string.

    """
    route = trigger.route
    snap = trigger.snapshot
    header = f"{route.origin} -> {route.destination}"

    lines = [header]

    if snap.return_date:
        lines.append(f"Departure: {snap.departure_date}, Return: {snap.return_date}")
    else:
        lines.append(f"Departure: {snap.departure_date}")

    lines.append(f"Price: {snap.currency} {snap.price:.2f}")

    if trigger.alert.alert_type == AlertType.DROP and trigger.previous_low is not None:
        drop_pct = (1 - snap.price / trigger.previous_low) * 100
        lines.append(
            f"Previous low: {snap.currency} {trigger.previous_low:.2f} (down {drop_pct:.1f}%)"
        )
    elif trigger.alert.alert_type == AlertType.THRESHOLD and trigger.alert.threshold is not None:
        lines.append(f"Threshold: {snap.currency} {trigger.alert.threshold:.2f}")

    lines.append(f"Cabin: {route.cabin_class}, Stops: {route.max_stops}")

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
    """Build a short notification title."""
    if trigger.alert.alert_type == AlertType.DROP:
        return f"Price Drop: {trigger.route.origin} -> {trigger.route.destination}"
    return f"Price Alert: {trigger.route.origin} -> {trigger.route.destination}"

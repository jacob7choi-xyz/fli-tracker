"""Tests for the notifier module.

Apprise is mocked to avoid sending real notifications. Tests verify
message formatting, notification delivery, logging, and error handling.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.detector import AlertTrigger
from fli.tracker.models import Alert, AlertType, PriceSnapshot, Route
from fli.tracker.notifier import format_message, notify_all, send_notification


@pytest.fixture()
def db(tmp_path: Path) -> TrackerDB:
    return TrackerDB(db_path=tmp_path / "test.db")


def _make_trigger(
    alert_type: AlertType = AlertType.DROP,
    price: float = 450.0,
    previous_low: float | None = 500.0,
    threshold: float | None = None,
    departure_date: str = "2026-07-15",
    return_date: str | None = "2026-07-22",
    origin: str = "DFW",
    destination: str = "FCO",
    alert_id: int = 1,
    route_id: int = 1,
) -> AlertTrigger:
    route = Route(id=route_id, origin=origin, destination=destination)
    alert = Alert(
        id=alert_id,
        route_id=route_id,
        alert_type=alert_type,
        threshold=threshold,
        notify_url="test://url",
    )
    snapshot = PriceSnapshot(
        route_id=route_id,
        departure_date=departure_date,
        return_date=return_date,
        price=price,
        currency="USD",
    )
    return AlertTrigger(
        alert=alert,
        route=route,
        snapshot=snapshot,
        previous_low=previous_low,
    )


# ------------------------------------------------------------------
# format_message
# ------------------------------------------------------------------


class TestFormatMessage:
    """Tests for notification message formatting."""

    def test_drop_message_format(self):
        trigger = _make_trigger(
            alert_type=AlertType.DROP,
            price=450.0,
            previous_low=500.0,
        )
        msg = format_message(trigger)

        assert "DFW -> FCO" in msg
        assert "2026-07-15" in msg
        assert "2026-07-22" in msg
        assert "450.00" in msg
        assert "500.00" in msg
        assert "10.0%" in msg

    def test_threshold_message_format(self):
        trigger = _make_trigger(
            alert_type=AlertType.THRESHOLD,
            price=480.0,
            threshold=500.0,
            previous_low=None,
        )
        msg = format_message(trigger)

        assert "DFW -> FCO" in msg
        assert "480.00" in msg
        assert "500.00" in msg
        assert "Threshold" in msg

    def test_one_way_message_no_return(self):
        trigger = _make_trigger(return_date=None)
        msg = format_message(trigger)

        assert "Return" not in msg
        assert "Departure: 2026-07-15" in msg

    def test_round_trip_message_shows_return(self):
        trigger = _make_trigger(return_date="2026-07-22")
        msg = format_message(trigger)

        assert "Return: 2026-07-22" in msg

    def test_message_includes_cabin_and_stops(self):
        trigger = _make_trigger()
        msg = format_message(trigger)

        assert "ECONOMY" in msg
        assert "ANY" in msg


# ------------------------------------------------------------------
# send_notification
# ------------------------------------------------------------------


class TestSendNotification:
    """Tests for notification delivery and logging."""

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_successful_send(self, mock_apprise_mod, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )

        trigger = _make_trigger(alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        result = send_notification(trigger, db)

        assert result is True
        mock_ap.add.assert_called_once_with("test://url")
        mock_ap.notify.assert_called_once()

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_failed_send_still_logs(self, mock_apprise_mod, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )

        trigger = _make_trigger(alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = False

        result = send_notification(trigger, db)

        assert result is False
        # Notification should still be logged for dedup
        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0) is True

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_logs_notification_record(self, mock_apprise_mod, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )

        trigger = _make_trigger(alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        send_notification(trigger, db)

        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0) is True

    @patch("fli.tracker.notifier._HAS_APPRISE", False)
    def test_missing_apprise_returns_false(self, db: TrackerDB):
        """If apprise is not installed, send_notification returns False."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )

        trigger = _make_trigger(alert_id=alert.id, route_id=route.id)
        result = send_notification(trigger, db)

        assert result is False


# ------------------------------------------------------------------
# notify_all
# ------------------------------------------------------------------


class TestNotifyAll:
    """Tests for batch notification delivery."""

    @patch("fli.tracker.notifier.send_notification")
    def test_sends_all_triggers(self, mock_send, db: TrackerDB):
        mock_send.return_value = True
        triggers = [_make_trigger(), _make_trigger(price=400.0)]

        sent = notify_all(triggers, db)

        assert sent == 2
        assert mock_send.call_count == 2

    @patch("fli.tracker.notifier.send_notification")
    def test_counts_only_successful(self, mock_send, db: TrackerDB):
        mock_send.side_effect = [True, False, True]
        triggers = [_make_trigger(), _make_trigger(price=400.0), _make_trigger(price=350.0)]

        sent = notify_all(triggers, db)

        assert sent == 2

    @patch("fli.tracker.notifier.send_notification")
    def test_empty_triggers(self, mock_send, db: TrackerDB):
        sent = notify_all([], db)
        assert sent == 0
        mock_send.assert_not_called()

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
from fli.tracker.notifier import (
    _build_search_url,
    _compute_nights,
    _format_perks,
    _rate_deal,
    format_message,
    notify_all,
    send_notification,
)


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
# Helper functions
# ------------------------------------------------------------------


class TestDealRating:
    """Tests for deal quality rating."""

    def test_domestic_insane(self):
        assert _rate_deal(80.0, "LAX") == "INSANE DEAL"

    def test_domestic_great(self):
        assert _rate_deal(120.0, "JFK") == "Great deal"

    def test_domestic_good(self):
        assert _rate_deal(200.0, "MIA") == "Good deal"

    def test_domestic_fair(self):
        assert _rate_deal(300.0, "ORD") == "Fair price"

    def test_europe_insane(self):
        assert _rate_deal(450.0, "FCO") == "INSANE DEAL"

    def test_europe_great(self):
        assert _rate_deal(600.0, "LHR") == "Great deal"

    def test_near_intl_insane(self):
        assert _rate_deal(150.0, "CUN") == "INSANE DEAL"

    def test_asia_great(self):
        assert _rate_deal(900.0, "NRT") == "Great deal"

    def test_unknown_destination_uses_other(self):
        assert _rate_deal(400.0, "XYZ") == "INSANE DEAL"


class TestComputeNights:
    """Tests for night computation."""

    def test_seven_nights(self):
        assert _compute_nights("2026-07-15", "2026-07-22") == 7

    def test_none_return_date(self):
        assert _compute_nights("2026-07-15", None) is None

    def test_invalid_date(self):
        assert _compute_nights("bad", "date") is None


class TestBuildSearchUrl:
    """Tests for Google Flights search URL generation."""

    def test_round_trip_url(self):
        url = _build_search_url("DFW", "FCO", "2026-07-15", "2026-07-22")
        assert "google.com/travel/flights" in url
        assert "DFW" in url
        assert "FCO" in url
        assert "2026-07-15" in url
        assert "2026-07-22" in url

    def test_one_way_url(self):
        url = _build_search_url("DFW", "LAX", "2026-07-15", None)
        assert "one%20way" in url


# ------------------------------------------------------------------
# Airline perks
# ------------------------------------------------------------------


class TestAirlinePerks:
    """Tests for airline perks formatting."""

    def test_full_service_us(self):
        perks = _format_perks(["AA"])
        assert "Free carry-on" in perks
        assert "Paid checked bag" in perks

    def test_budget_us(self):
        perks = _format_perks(["NK"])
        assert "Paid carry-on" in perks
        assert "Paid checked bag" in perks

    def test_international_full_service(self):
        perks = _format_perks(["JL"])
        assert "Free carry-on" in perks
        assert "Free checked bag" in perks
        assert "Free seat selection" in perks

    def test_unknown_airline(self):
        assert _format_perks(["ZZ"]) is None

    def test_empty_list(self):
        assert _format_perks([]) is None

    def test_uses_primary_airline(self):
        """Perks are based on the first (primary) airline."""
        perks = _format_perks(["DL", "NK"])
        assert "Free carry-on" in perks


# ------------------------------------------------------------------
# format_message
# ------------------------------------------------------------------


class TestFormatMessage:
    """Tests for notification message formatting."""

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_drop_message_format(self, _mock_details):
        trigger = _make_trigger(
            alert_type=AlertType.DROP,
            price=450.0,
            previous_low=500.0,
        )
        msg = format_message(trigger)

        assert "DFW -> FCO" in msg
        assert "2026-07-15" in msg
        assert "2026-07-22" in msg
        assert "$450" in msg
        assert "$500" in msg
        assert "10.0%" in msg
        assert "New all-time low" in msg
        assert "Book now:" in msg

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_threshold_message_format(self, _mock_details):
        trigger = _make_trigger(
            alert_type=AlertType.THRESHOLD,
            price=480.0,
            threshold=500.0,
            previous_low=None,
        )
        msg = format_message(trigger)

        assert "DFW -> FCO" in msg
        assert "$480" in msg
        assert "$500" in msg
        assert "Threshold" in msg
        assert "Threshold hit" in msg

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_one_way_message_no_return(self, _mock_details):
        trigger = _make_trigger(return_date=None)
        msg = format_message(trigger)

        assert "one-way" in msg
        assert "Departure: 2026-07-15" in msg

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_round_trip_includes_dates_and_nights(self, _mock_details):
        trigger = _make_trigger(return_date="2026-07-22")
        msg = format_message(trigger)

        assert "2026-07-15 -> 2026-07-22" in msg
        assert "7 nights" in msg

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_fallback_shows_cabin_and_stops(self, _mock_details):
        """When flight details are unavailable, shows cabin and stops from route."""
        trigger = _make_trigger()
        msg = format_message(trigger)

        assert "ECONOMY" in msg
        assert "ANY" in msg

    @patch(
        "fli.tracker.notifier._fetch_flight_details",
        return_value=(
            "  Outbound:\n"
            "    Airlines: AA\n"
            "    Duration: 10h 25m (Nonstop)\n"
            "    Times: 06:40 PM -> 12:05 PM\n"
            "    Perks: Free carry-on, Paid checked bag\n"
            "  Return:\n"
            "    Airlines: AA\n"
            "    Duration: 11h 10m (Nonstop)\n"
            "    Times: 01:30 PM -> 06:40 PM\n"
            "    Perks: Free carry-on, Paid checked bag"
        ),
    )
    def test_with_flight_details(self, _mock_details):
        """When flight details are available, shows both legs with perks."""
        trigger = _make_trigger()
        msg = format_message(trigger)

        assert "Outbound:" in msg
        assert "Return:" in msg
        assert "Airlines: AA" in msg
        assert "10h 25m" in msg
        assert "Nonstop" in msg
        assert "Perks:" in msg
        assert "Free carry-on" in msg
        assert "ECONOMY" not in msg

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_deal_quality_in_message(self, _mock_details):
        trigger = _make_trigger(price=450.0, destination="FCO")
        msg = format_message(trigger)
        assert "Deal quality:" in msg

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    def test_search_link_in_message(self, _mock_details):
        trigger = _make_trigger()
        msg = format_message(trigger)
        assert "google.com/travel/flights" in msg


# ------------------------------------------------------------------
# send_notification
# ------------------------------------------------------------------


class TestSendNotification:
    """Tests for notification delivery and logging."""

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_successful_send(self, mock_apprise_mod, _mock_details, db: TrackerDB):
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

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_failed_send_still_logs(self, mock_apprise_mod, _mock_details, db: TrackerDB):
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

    @patch("fli.tracker.notifier._fetch_flight_details", return_value=None)
    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_logs_notification_record(self, mock_apprise_mod, _mock_details, db: TrackerDB):
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

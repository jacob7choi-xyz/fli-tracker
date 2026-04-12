"""Tests for the notifier module.

Apprise is mocked to avoid sending real notifications. Tests verify
message formatting, notification delivery, logging, and error handling.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.detector import AlertTrigger
from fli.tracker.models import Alert, AlertType, MonthlyStats, PriceSnapshot, Route, RouteStats
from fli.tracker.notifier import (
    _build_search_url,
    _build_title,
    _compute_nights,
    _format_perks,
    _score_deal,
    format_digest,
    format_message,
    notify_all,
    send_digest,
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
    max_price: float | None = None,
) -> AlertTrigger:
    route = Route(id=route_id, origin=origin, destination=destination, max_price=max_price)
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


def _make_stats(
    all_time_min: float = 400.0,
    overall_median: float = 600.0,
    total_count: int = 50,
    days_of_history: int = 30,
    monthly: dict[int, MonthlyStats] | None = None,
) -> RouteStats:
    """Build RouteStats for scoring tests."""
    if monthly is None:
        monthly = {7: MonthlyStats(avg_price=600.0, count=10)}
    return RouteStats(
        all_time_min=all_time_min,
        overall_median=overall_median,
        total_count=total_count,
        days_of_history=days_of_history,
        monthly=monthly,
    )


class TestScoreDeal:
    """Tests for data-driven deal scoring."""

    @pytest.mark.parametrize(
        ("stats", "expected"),
        [
            pytest.param(None, "Building history...", id="no_stats"),
            pytest.param(
                _make_stats(total_count=5),
                "Building history...",
                id="insufficient_snapshots",
            ),
            pytest.param(
                _make_stats(days_of_history=7),
                "Building history...",
                id="insufficient_days",
            ),
        ],
    )
    def test_confidence_gate(self, stats, expected):
        assert _score_deal(450.0, stats, 7) == expected

    def test_price_at_median_scores_low(self):
        stats = _make_stats(overall_median=500.0, all_time_min=400.0)
        result = _score_deal(500.0, stats, 7)
        assert result in ("Fair price", "Decent")

    def test_price_well_below_median_scores_high(self):
        stats = _make_stats(
            overall_median=800.0,
            all_time_min=350.0,
            monthly={7: MonthlyStats(avg_price=800.0, count=10)},
        )
        result = _score_deal(350.0, stats, 7)
        assert result in ("INSANE DEAL", "Great deal")

    def test_new_all_time_low_gets_bonus(self):
        stats = _make_stats(
            overall_median=700.0,
            all_time_min=450.0,
            monthly={7: MonthlyStats(avg_price=700.0, count=10)},
        )
        result = _score_deal(400.0, stats, 7)
        assert result in ("INSANE DEAL", "Great deal")

    def test_price_above_median_is_fair(self):
        stats = _make_stats(overall_median=500.0, all_time_min=400.0)
        assert _score_deal(600.0, stats, 7) == "Fair price"

    def test_peak_month_bonus(self):
        """Cheap fare in a peak month (July) should score higher than shoulder."""
        stats = _make_stats(
            overall_median=700.0,
            all_time_min=400.0,
            monthly={
                7: MonthlyStats(avg_price=750.0, count=10),
                3: MonthlyStats(avg_price=500.0, count=10),
            },
        )
        labels = ["Fair price", "Decent", "Good deal", "Great deal", "INSANE DEAL"]
        assert labels.index(_score_deal(400.0, stats, 7)) >= labels.index(
            _score_deal(400.0, stats, 3)
        )

    def test_thin_month_data_skips_seasonality(self):
        """Month with < 5 samples should not contribute to seasonality score."""
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            monthly={7: MonthlyStats(avg_price=700.0, count=2)},
        )
        assert _score_deal(400.0, stats, 7) != "Building history..."

    def test_decile_gated_on_sample_size(self):
        """Bottom decile scoring should be disabled with < 20 observations."""
        stats_small = _make_stats(total_count=15, all_time_min=400.0, overall_median=600.0)
        stats_large = _make_stats(total_count=50, all_time_min=400.0, overall_median=600.0)
        labels = ["Fair price", "Decent", "Good deal", "Great deal", "INSANE DEAL"]
        assert labels.index(_score_deal(400.0, stats_small, 7)) <= labels.index(
            _score_deal(400.0, stats_large, 7)
        )

    def test_no_departure_month(self):
        """None departure month should still score without seasonality."""
        stats = _make_stats()
        assert _score_deal(400.0, stats, None) != "Building history..."


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
        stats = _make_stats(overall_median=700.0, all_time_min=400.0)
        msg = format_message(trigger, _stats=stats)
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
        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is True

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

        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is True

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


# ------------------------------------------------------------------
# _build_title
# ------------------------------------------------------------------


class TestBuildTitle:
    """Tests for notification title generation."""

    def test_drop_title_with_stats(self):
        trigger = _make_trigger(alert_type=AlertType.DROP, price=350.0)
        stats = _make_stats(overall_median=700.0, all_time_min=400.0)
        title = _build_title(trigger, _stats=stats)
        assert "Price Drop" in title
        assert "DFW -> FCO" in title

    def test_threshold_title_with_stats(self):
        trigger = _make_trigger(
            alert_type=AlertType.THRESHOLD, price=480.0, threshold=500.0
        )
        stats = _make_stats()
        title = _build_title(trigger, _stats=stats)
        assert "Price Alert" in title
        assert "DFW -> FCO" in title

    def test_title_without_stats(self):
        trigger = _make_trigger()
        title = _build_title(trigger, _stats=None)
        assert "Building history..." in title

    @pytest.mark.parametrize(
        ("alert_type", "expected_prefix"),
        [
            pytest.param(AlertType.DROP, "Price Drop", id="drop"),
            pytest.param(AlertType.THRESHOLD, "Price Alert", id="threshold"),
        ],
    )
    def test_title_prefix_by_type(self, alert_type, expected_prefix):
        trigger = _make_trigger(alert_type=alert_type, threshold=500.0)
        title = _build_title(trigger)
        assert title.startswith(expected_prefix)


# ------------------------------------------------------------------
# Digest
# ------------------------------------------------------------------


class TestFormatDigest:
    """Tests for digest email formatting."""

    def test_single_trigger_digest(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        trigger = _make_trigger(route_id=route.id)
        body = format_digest([trigger], db)
        assert "1 deal - best" in body
        assert "Dallas-Fort Worth, TX" in body
        assert "Rome, Italy" in body
        assert "$450" in body
        assert "Book on Google Flights" in body
        assert "google.com/travel/flights" in body

    def test_multiple_triggers_sorted_by_price(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        t1 = _make_trigger(price=600.0, route_id=route.id)
        t2 = _make_trigger(price=300.0, route_id=route.id)
        body = format_digest([t1, t2], db)
        assert "2 deals - best" in body
        # Cheaper should appear first (lower index in string)
        assert body.index("$300") < body.index("$600")

    def test_drops_before_thresholds(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        drop = _make_trigger(
            alert_type=AlertType.DROP, price=500.0, route_id=route.id
        )
        threshold = _make_trigger(
            alert_type=AlertType.THRESHOLD,
            price=400.0,
            threshold=600.0,
            previous_low=None,
            route_id=route.id,
        )
        body = format_digest([threshold, drop], db)
        # Drop should appear before threshold even though threshold is cheaper
        assert body.index("New low") < body.index("threshold")

    def test_digest_includes_deal_rating(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        trigger = _make_trigger(route_id=route.id)
        body = format_digest([trigger], db)
        # Should have some deal label (Building history... since no stats)
        assert "Building history..." in body

    def test_empty_triggers_returns_header_only(self, db: TrackerDB):
        body = format_digest([], db)
        assert "No deals" in body


class TestSendDigest:
    """Tests for digest delivery."""

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_sends_one_email_for_multiple_triggers(self, mock_apprise_mod, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        t1 = _make_trigger(price=450.0, alert_id=alert.id, route_id=route.id)
        t2 = _make_trigger(
            price=400.0, alert_id=alert.id, route_id=route.id,
            departure_date="2026-07-20",
        )

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        sent = send_digest([t1, t2], db)

        assert sent == 2
        # Only ONE email sent (one call to notify)
        mock_ap.notify.assert_called_once()

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_logs_each_trigger_for_dedup(self, mock_apprise_mod, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        t1 = _make_trigger(price=450.0, alert_id=alert.id, route_id=route.id)
        t2 = _make_trigger(
            price=400.0, alert_id=alert.id, route_id=route.id,
            departure_date="2026-07-20",
        )

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        send_digest([t1, t2], db)

        # Both triggers should be logged for dedup
        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is True
        assert db.was_notification_sent(alert.id, "2026-07-20", 400.0, "2026-07-22") is True

    def test_empty_triggers_returns_zero(self, db: TrackerDB):
        assert send_digest([], db) == 0

    @patch("fli.tracker.notifier._HAS_APPRISE", False)
    def test_no_apprise_returns_zero(self, db: TrackerDB):
        trigger = _make_trigger()
        assert send_digest([trigger], db) == 0

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_max_price_filters_expensive_triggers(self, mock_apprise_mod, db: TrackerDB):
        """Triggers above route max_price are excluded from the digest."""
        route = db.add_route(Route(origin="DFW", destination="NRT", max_price=900.0))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        cheap = _make_trigger(
            price=850.0, alert_id=alert.id, route_id=route.id, max_price=900.0,
        )
        expensive = _make_trigger(
            price=1100.0, alert_id=alert.id, route_id=route.id,
            departure_date="2026-08-01", max_price=900.0,
        )

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        sent = send_digest([cheap, expensive], db)

        # Only the cheap trigger should be sent
        assert sent == 1
        mock_ap.notify.assert_called_once()

    def test_all_triggers_above_max_price_returns_zero(self, db: TrackerDB):
        """If all triggers exceed max_price, nothing is sent."""
        trigger = _make_trigger(price=1000.0, max_price=800.0)
        assert send_digest([trigger], db) == 0


class TestAirportLocations:
    """Tests for airport location metadata."""

    def test_csv_loads_successfully(self):
        from fli.tracker.notifier import _AIRPORT_LOCATIONS

        assert len(_AIRPORT_LOCATIONS) > 0

    def test_domestic_format_city_state(self):
        from fli.tracker.notifier import _AIRPORT_LOCATIONS

        assert _AIRPORT_LOCATIONS["DFW"] == "Dallas-Fort Worth, TX"
        assert _AIRPORT_LOCATIONS["BOS"] == "Boston, MA"

    def test_international_format_city_country(self):
        from fli.tracker.notifier import _AIRPORT_LOCATIONS

        assert _AIRPORT_LOCATIONS["FCO"] == "Rome, Italy"
        assert _AIRPORT_LOCATIONS["NRT"] == "Tokyo Narita, Japan"

    def test_fallback_for_unknown_code(self):
        from fli.tracker.notifier import _format_route_label

        # Unknown code falls back to the bare IATA code
        label = _format_route_label("ZZZ", "YYY")
        assert "ZZZ" in label
        assert "YYY" in label

    def test_tracked_airports_have_metadata(self):
        """Every airport in the current tracked routes has location metadata."""
        from fli.tracker.notifier import _AIRPORT_LOCATIONS

        tracked_codes = [
            "DFW", "ATL", "ORD", "LAX", "SFO", "SEA", "MIA", "BOS", "JFK",
            "DEN", "MSP", "PHX", "HNL", "ANC", "PWM",
            "CUN", "SJO",
            "LHR", "CDG", "FCO", "BCN", "LIS", "AMS", "DUB", "ATH", "IST",
            "NRT", "ICN", "BKK",
            "SYD", "AKL", "GRU", "EZE", "BOG", "SCL",
        ]
        missing = [c for c in tracked_codes if c not in _AIRPORT_LOCATIONS]
        assert missing == [], f"Missing airport metadata for: {missing}"

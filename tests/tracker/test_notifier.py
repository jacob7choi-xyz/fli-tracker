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
    LegDetail,
    NotificationConfigError,
    NotificationDeliveryError,
    NotificationDependencyError,
    _build_search_url,
    _build_title,
    _build_trend_line,
    _compute_nights,
    _compute_percentile_rank,
    _format_perks,
    _is_domestic_route,
    _lead_time_bucket,
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
    price_mean: float | None = None,
    price_stddev: float | None = None,
    price_percentiles: dict[int, float] | None = None,
    lead_time_buckets: dict[str, float] | None = None,
) -> RouteStats:
    """Build RouteStats for scoring tests with sensible defaults for new fields."""
    if monthly is None:
        monthly = {7: MonthlyStats(avg_price=600.0, count=10)}
    if price_mean is None:
        price_mean = overall_median
    if price_stddev is None:
        price_stddev = overall_median * 0.15
    if price_percentiles is None:
        spread = overall_median - all_time_min
        price_percentiles = {
            10: all_time_min,
            25: all_time_min + spread * 0.3,
            50: overall_median,
            75: overall_median + spread * 0.5,
            90: overall_median + spread * 0.9,
        }
    return RouteStats(
        all_time_min=all_time_min,
        overall_median=overall_median,
        total_count=total_count,
        days_of_history=days_of_history,
        monthly=monthly,
        price_mean=price_mean,
        price_stddev=price_stddev,
        price_percentiles=price_percentiles,
        lead_time_buckets=lead_time_buckets or {},
    )


class TestLeadTimeBucket:
    """Tests for lead-time bucket assignment."""

    @pytest.mark.parametrize(
        ("lead_days", "expected"),
        [
            (-1, None),
            (0, "0-7"),
            (7, "0-7"),
            (8, "8-14"),
            (14, "8-14"),
            (15, "15-30"),
            (30, "15-30"),
            (31, "31-60"),
            (60, "31-60"),
            (61, None),
            (90, None),
        ],
    )
    def test_bucket_assignment(self, lead_days, expected):
        assert _lead_time_bucket(lead_days) == expected


class TestComputePercentileRank:
    """Tests for interpolated percentile rank estimation."""

    PERCENTILES = {10: 100.0, 25: 200.0, 50: 400.0, 75: 600.0, 90: 750.0}

    @pytest.mark.parametrize(
        ("price", "expected_range"),
        [
            (50.0, (0.0, 10.0)),  # below p10
            (100.0, (10.0, 10.0)),  # at p10
            (150.0, (10.0, 25.0)),  # between p10 and p25
            (200.0, (25.0, 25.0)),  # at p25
            (400.0, (50.0, 50.0)),  # at p50 (median)
            (600.0, (75.0, 75.0)),  # at p75
            (800.0, (90.0, 100.0)),  # above p90
        ],
    )
    def test_rank_in_expected_range(self, price, expected_range):
        rank = _compute_percentile_rank(price, self.PERCENTILES)
        lo, hi = expected_range
        assert lo <= rank <= hi, f"price={price} rank={rank} not in [{lo}, {hi}]"

    def test_empty_percentiles_returns_midpoint(self):
        assert _compute_percentile_rank(500.0, {}) == 50.0

    def test_rank_monotone_with_price(self):
        """Higher price should yield higher (or equal) percentile rank."""
        prices = [100.0, 200.0, 300.0, 400.0, 600.0, 750.0, 900.0]
        ranks = [_compute_percentile_rank(p, self.PERCENTILES) for p in prices]
        for i in range(len(ranks) - 1):
            assert ranks[i] <= ranks[i + 1]


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
        assert _score_deal(450.0, stats, "2026-07-15") == expected

    def test_price_at_median_scores_low(self):
        stats = _make_stats(overall_median=500.0, all_time_min=400.0)
        result = _score_deal(500.0, stats, "2026-07-15")
        assert result in ("Skip", "Meh")

    def test_price_well_below_median_scores_high(self):
        stats = _make_stats(
            overall_median=800.0,
            all_time_min=350.0,
            monthly={7: MonthlyStats(avg_price=800.0, count=10)},
        )
        result = _score_deal(350.0, stats, "2026-07-15")
        assert result in ("BUY NOW", "Strong buy")

    def test_price_well_below_mean_gets_high_z_score(self):
        """Price 2+ std devs below mean hits the z-score ceiling."""
        stats = _make_stats(
            overall_median=700.0,
            all_time_min=450.0,
            price_mean=700.0,
            price_stddev=100.0,
        )
        result = _score_deal(400.0, stats, "2026-07-15")
        assert result in ("BUY NOW", "Strong buy")

    def test_price_above_median_scores_skip(self):
        stats = _make_stats(overall_median=500.0, all_time_min=400.0)
        assert _score_deal(600.0, stats, "2026-07-15") == "Skip"

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
        labels = ["Skip", "Meh", "Worth watching", "Strong buy", "BUY NOW"]
        assert labels.index(_score_deal(400.0, stats, "2026-07-15")) >= labels.index(
            _score_deal(400.0, stats, "2026-03-15")
        )

    def test_thin_month_data_skips_seasonality(self):
        """Month with < 5 samples should not contribute to seasonality score."""
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            monthly={7: MonthlyStats(avg_price=700.0, count=2)},
        )
        assert _score_deal(400.0, stats, "2026-07-15") != "Building history..."

    def test_no_departure_date(self):
        """None departure date should still score using z-score and percentile only."""
        stats = _make_stats()
        assert _score_deal(400.0, stats, None) != "Building history..."

    def test_lead_time_component_raises_score(self):
        """Price below the lead-time bucket average adds to the score."""
        bucket_avg = 600.0
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            price_mean=600.0,
            price_stddev=90.0,
            lead_time_buckets={"31-60": bucket_avg},
        )
        # Score with a lead-time discount (31-60 days out, price well below bucket avg)
        from datetime import date, timedelta

        dep_date = (date.today() + timedelta(days=45)).isoformat()
        result_with_lt = _score_deal(400.0, stats, dep_date)
        result_no_lt = _score_deal(
            400.0,
            _make_stats(
                overall_median=600.0,
                all_time_min=400.0,
                price_mean=600.0,
                price_stddev=90.0,
            ),
            dep_date,
        )
        labels = ["Skip", "Meh", "Worth watching", "Strong buy", "BUY NOW"]
        assert labels.index(result_with_lt) >= labels.index(result_no_lt)


class TestBuildTrendLine:
    """Tests for the compact per-deal trend summary."""

    @pytest.mark.parametrize(
        "stats",
        [
            pytest.param(None, id="no_stats"),
            pytest.param(_make_stats(total_count=5), id="insufficient_snapshots"),
            pytest.param(_make_stats(days_of_history=7), id="insufficient_days"),
        ],
    )
    def test_returns_none_when_insufficient_data(self, stats):
        assert _build_trend_line(450.0, stats, "2026-07-15") is None

    def test_below_median(self):
        stats = _make_stats(overall_median=600.0, all_time_min=300.0)
        result = _build_trend_line(400.0, stats, None)
        assert result is not None
        assert "below median" in result

    def test_above_median(self):
        stats = _make_stats(overall_median=400.0, all_time_min=300.0)
        result = _build_trend_line(500.0, stats, None)
        assert result is not None
        assert "above median" in result

    def test_at_median(self):
        stats = _make_stats(overall_median=450.0, all_time_min=300.0)
        result = _build_trend_line(450.0, stats, None)
        assert result is not None
        assert "at median" in result

    def test_new_all_time_low(self):
        stats = _make_stats(overall_median=600.0, all_time_min=450.0, days_of_history=30)
        result = _build_trend_line(400.0, stats, None)
        assert result is not None
        assert "new 30d low" in result

    def test_near_all_time_low(self):
        stats = _make_stats(overall_median=600.0, all_time_min=400.0)
        # 415 / 400 = 1.0375, within 5%
        result = _build_trend_line(415.0, stats, None)
        assert result is not None
        assert "near all-time low" in result
        assert "$400" in result

    def test_above_all_time_low(self):
        stats = _make_stats(overall_median=600.0, all_time_min=300.0)
        result = _build_trend_line(500.0, stats, None)
        assert result is not None
        assert "$200 above all-time low" in result

    def test_monthly_context_shown_when_significant(self):
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            monthly={7: MonthlyStats(avg_price=700.0, count=10)},
        )
        # 560 is 20% below Jul avg of 700 -- should appear
        result = _build_trend_line(560.0, stats, "2026-07-15")
        assert result is not None
        assert "Jul" in result

    def test_monthly_context_omitted_when_small_discount(self):
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            monthly={7: MonthlyStats(avg_price=620.0, count=10)},
        )
        # 580 is ~6% below 620 -- below 10% threshold, should be omitted
        result = _build_trend_line(580.0, stats, "2026-07-15")
        assert result is None or "Jul" not in result

    def test_percentile_rank_shown_in_bottom_quartile(self):
        """Prices in the bottom 25% should show percentile context."""
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            price_percentiles={10: 420.0, 25: 470.0, 50: 600.0, 75: 720.0, 90: 800.0},
        )
        # 450 is between p10 and p25 -- bottom ~20th percentile
        result = _build_trend_line(450.0, stats, None)
        assert result is not None
        assert "bottom" in result and "%" in result

    def test_percentile_rank_omitted_above_quartile(self):
        """Prices above the 25th percentile should not show percentile context."""
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            price_percentiles={10: 420.0, 25: 470.0, 50: 600.0, 75: 720.0, 90: 800.0},
        )
        # 550 is between p25 and p50 -- not in bottom quartile
        result = _build_trend_line(550.0, stats, None)
        assert result is None or "bottom" not in result

    def test_monthly_context_omitted_when_thin_data(self):
        stats = _make_stats(
            overall_median=600.0,
            all_time_min=400.0,
            monthly={7: MonthlyStats(avg_price=700.0, count=2)},
        )
        result = _build_trend_line(400.0, stats, "2026-07-15")
        assert result is None or "Jul" not in result

    def test_parts_joined_by_semicolon(self):
        stats = _make_stats(overall_median=600.0, all_time_min=400.0)
        result = _build_trend_line(400.0, stats, None)
        assert result is not None
        assert "; " in result


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

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
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

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
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

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_one_way_message_no_return(self, _mock_details):
        trigger = _make_trigger(return_date=None)
        msg = format_message(trigger)

        assert "one-way" in msg
        assert "Departure: 2026-07-15" in msg

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_round_trip_includes_dates_and_nights(self, _mock_details):
        trigger = _make_trigger(return_date="2026-07-22")
        msg = format_message(trigger)

        assert "2026-07-15 -> 2026-07-22" in msg
        assert "7 nights" in msg

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_fallback_shows_cabin_and_stops(self, _mock_details):
        """When flight details are unavailable, shows cabin and stops from route."""
        trigger = _make_trigger()
        msg = format_message(trigger)

        assert "ECONOMY" in msg
        assert "ANY" in msg

    @patch(
        "fli.tracker.notifier.fetch_flight_details",
        return_value=[
            LegDetail(
                label="Outbound",
                airlines="AA",
                duration="10h 25m",
                stops="Nonstop",
                dep_time="06:40 PM",
                arr_time="12:05 PM",
                perks="Free carry-on, Paid checked bag",
            ),
            LegDetail(
                label="Return",
                airlines="AA",
                duration="11h 10m",
                stops="Nonstop",
                dep_time="01:30 PM",
                arr_time="06:40 PM",
                perks="Free carry-on, Paid checked bag",
            ),
        ],
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

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_deal_quality_in_message(self, _mock_details):
        trigger = _make_trigger(price=450.0, destination="FCO")
        stats = _make_stats(overall_median=700.0, all_time_min=400.0)
        msg = format_message(trigger, _stats=stats)
        assert "Deal quality:" in msg

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_search_link_in_message(self, _mock_details):
        trigger = _make_trigger()
        msg = format_message(trigger)
        assert "google.com/travel/flights" in msg

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_trend_line_shown_when_stats_available(self, _mock_details):
        trigger = _make_trigger(price=400.0)
        stats = _make_stats(overall_median=600.0, all_time_min=300.0)
        msg = format_message(trigger, _stats=stats)
        assert "Trend:" in msg

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    def test_no_trend_line_when_insufficient_stats(self, _mock_details):
        trigger = _make_trigger(price=400.0)
        stats = _make_stats(total_count=5)  # below _MIN_SNAPSHOTS threshold
        msg = format_message(trigger, _stats=stats)
        assert "Trend:" not in msg


# ------------------------------------------------------------------
# send_notification
# ------------------------------------------------------------------


class TestSendNotification:
    """Tests for notification delivery and logging."""

    @pytest.fixture(autouse=True)
    def notify_env(self, monkeypatch):
        """Notification target comes from the environment, never the DB."""
        monkeypatch.setenv("NOTIFY_URL", "test://url")

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
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

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_failed_send_raises_and_stays_retry_eligible(
        self, mock_apprise_mod, _mock_details, db: TrackerDB
    ):
        """A failed delivery must NOT be recorded as sent.

        A suppression row for a message the user never received would
        permanently silence the fare event. Failure raises, no dedup row
        is written, and the next sweep may retry (false duplicate over
        false suppression).
        """
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

        with pytest.raises(NotificationDeliveryError):
            send_notification(trigger, db)

        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is False

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
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

        # Storage contract: message is EXACTLY the constant marker. The column
        # may never carry rendered content (2026-07 DB bloat incident).
        rows = db._conn.execute("SELECT message FROM notification_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["message"] == "single"

    @patch("fli.tracker.notifier._HAS_APPRISE", False)
    def test_missing_apprise_raises(self, db: TrackerDB):
        """A missing dependency is a lost capability, not a skipped feature."""
        trigger = _make_trigger()
        with pytest.raises(NotificationDependencyError):
            send_notification(trigger, db)


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
    def test_continues_past_failures_and_counts_delivered(self, mock_send, db: TrackerDB):
        mock_send.side_effect = [True, NotificationDeliveryError("failed"), True]
        triggers = [_make_trigger(), _make_trigger(price=400.0), _make_trigger(price=350.0)]

        sent = notify_all(triggers, db)

        assert sent == 2
        assert mock_send.call_count == 3

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
        trigger = _make_trigger(alert_type=AlertType.THRESHOLD, price=480.0, threshold=500.0)
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
        drop = _make_trigger(alert_type=AlertType.DROP, price=500.0, route_id=route.id)
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

    def test_domestic_and_international_sections(self, db: TrackerDB):
        """Digest splits triggers into Domestic and International sections."""
        dom_route = db.add_route(Route(origin="DFW", destination="ORD"))
        db.add_alert(
            Alert(route_id=dom_route.id, alert_type=AlertType.DROP, notify_url="test://url")
        )
        intl_route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(route_id=intl_route.id, alert_type=AlertType.DROP, notify_url="test://url")
        )
        dom_trigger = _make_trigger(
            price=100.0,
            route_id=dom_route.id,
            origin="DFW",
            destination="ORD",
        )
        intl_trigger = _make_trigger(
            price=500.0,
            route_id=intl_route.id,
            origin="DFW",
            destination="FCO",
        )
        body = format_digest([dom_trigger, intl_trigger], db)
        assert "Domestic Deals" in body
        assert "International Deals" in body
        # Domestic section appears before International
        assert body.index("Domestic Deals") < body.index("International Deals")

    def test_only_domestic_no_international_header(self, db: TrackerDB):
        """When all triggers are domestic, no International header appears."""
        route = db.add_route(Route(origin="DFW", destination="ORD"))
        db.add_alert(Alert(route_id=route.id, alert_type=AlertType.DROP, notify_url="test://url"))
        trigger = _make_trigger(
            price=100.0,
            route_id=route.id,
            origin="DFW",
            destination="ORD",
        )
        body = format_digest([trigger], db)
        assert "Domestic Deals" in body
        assert "International Deals" not in body

    def test_only_international_no_domestic_header(self, db: TrackerDB):
        """When all triggers are international, no Domestic header appears."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(Alert(route_id=route.id, alert_type=AlertType.DROP, notify_url="test://url"))
        trigger = _make_trigger(route_id=route.id)
        body = format_digest([trigger], db)
        assert "International Deals" in body
        assert "Domestic Deals" not in body

    def test_empty_triggers_returns_header_only(self, db: TrackerDB):
        body = format_digest([], db)
        assert "No deals" in body


class TestSendDigest:
    """Tests for digest delivery."""

    @pytest.fixture(autouse=True)
    def notify_env(self, monkeypatch):
        """Notification target comes from the environment, never the DB."""
        monkeypatch.setenv("NOTIFY_URL", "test://url")

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
            price=400.0,
            alert_id=alert.id,
            route_id=route.id,
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
            price=400.0,
            alert_id=alert.id,
            route_id=route.id,
            departure_date="2026-07-20",
        )

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        send_digest([t1, t2], db)

        # Both triggers should be logged for dedup
        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is True
        assert db.was_notification_sent(alert.id, "2026-07-20", 400.0, "2026-07-22") is True

        # Storage contract: message is EXACTLY the constant marker. The column
        # may never carry rendered content (2026-07 DB bloat incident).
        rows = db._conn.execute("SELECT message FROM notification_log").fetchall()
        assert len(rows) == 2
        assert all(row["message"] == "digest" for row in rows)

    def test_empty_triggers_returns_zero(self, db: TrackerDB):
        assert send_digest([], db) == 0

    @patch("fli.tracker.notifier._HAS_APPRISE", False)
    def test_no_apprise_raises(self, db: TrackerDB):
        trigger = _make_trigger()
        with pytest.raises(NotificationDependencyError):
            send_digest([trigger], db)

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
            price=850.0,
            alert_id=alert.id,
            route_id=route.id,
            max_price=900.0,
        )
        expensive = _make_trigger(
            price=1100.0,
            alert_id=alert.id,
            route_id=route.id,
            departure_date="2026-08-01",
            max_price=900.0,
        )

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        sent = send_digest([cheap, expensive], db)

        # Only the cheap trigger should be sent
        assert sent == 1
        mock_ap.notify.assert_called_once()

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_delivery_failure_raises_and_writes_no_dedup(self, mock_apprise_mod, db: TrackerDB):
        """A failed digest must leave every fare event retry-eligible.

        was_notification_sent must mean the user was actually told; writing
        suppression rows for undelivered digests would permanently silence
        the deals (2026-07 incident review finding).
        """
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        trigger = _make_trigger(price=450.0, alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = False

        with pytest.raises(NotificationDeliveryError):
            send_digest([trigger], db)

        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is False
        rows = db._conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        assert rows == 0

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_retry_after_failure_delivers_and_logs(self, mock_apprise_mod, db: TrackerDB):
        """The sweep after a failed delivery can send the same fare event."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        trigger = _make_trigger(price=450.0, alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = False
        with pytest.raises(NotificationDeliveryError):
            send_digest([trigger], db)

        mock_ap.notify.return_value = True
        sent = send_digest([trigger], db)

        assert sent == 1
        assert db.was_notification_sent(alert.id, "2026-07-15", 450.0, "2026-07-22") is True

    def test_all_triggers_above_max_price_returns_zero(self, db: TrackerDB):
        """If all triggers exceed max_price, nothing is sent."""
        trigger = _make_trigger(price=1000.0, max_price=800.0)
        assert send_digest([trigger], db) == 0


class TestFailClosedCredentials:
    """NOTIFY_URL is resolved from the environment only, failing closed.

    There is no fallback to alerts.notify_url: persisting credentials in the
    database is the incident class this architecture removes.
    """

    @pytest.fixture(autouse=True)
    def no_notify_env(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_URL", raising=False)

    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_send_notification_raises_without_env(
        self, mock_apprise_mod, _mock_details, db: TrackerDB
    ):
        trigger = _make_trigger()
        with pytest.raises(NotificationConfigError):
            send_notification(trigger, db)

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_send_digest_raises_without_env(self, mock_apprise_mod, db: TrackerDB):
        trigger = _make_trigger()
        with pytest.raises(NotificationConfigError):
            send_digest([trigger], db)

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_config_error_does_not_log_dedup_rows(self, mock_apprise_mod, db: TrackerDB):
        """A config error must not suppress future delivery of the same fare.

        Dedup rows are only written after the target resolves; otherwise the
        fare event would be permanently silenced once config is fixed.
        """
        trigger = _make_trigger()
        with pytest.raises(NotificationConfigError):
            send_digest([trigger], db)

        assert db.was_notification_sent(1, "2026-07-15", 450.0, "2026-07-22") is False

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_db_stored_url_is_never_used(self, mock_apprise_mod, monkeypatch, db: TrackerDB):
        """Even with a URL persisted on the alert, only the env value is used."""
        monkeypatch.setenv("NOTIFY_URL", "env://target")
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(route_id=route.id, alert_type=AlertType.DROP, notify_url="db://stored")
        )
        trigger = _make_trigger(alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = True

        send_digest([trigger], db)

        mock_ap.add.assert_called_once_with("env://target")


class TestSecretObservabilityBoundaries:
    """The credential crosses no persistence or observability boundary.

    Sentinel bytes from NOTIFY_URL must be absent from the DB file, captured
    stdout/stderr, and log records, on both delivery success and failure.
    """

    SECRET = "SENTINEL-c9f2-SECRET"
    SENTINEL_URL = f"mailto://sentinel:{SECRET}@example.com"

    @pytest.mark.parametrize("delivery_ok", [True, False])
    @patch("fli.tracker.notifier.fetch_flight_details", return_value=[])
    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_digest_flow_leaks_no_secret(
        self,
        mock_apprise_mod,
        _mock_details,
        delivery_ok,
        monkeypatch,
        tmp_path,
        capsys,
        caplog,
    ):
        monkeypatch.setenv("NOTIFY_URL", self.SENTINEL_URL)
        db_path = tmp_path / "sentinel.db"
        db = TrackerDB(db_path=db_path)
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(Alert(route_id=route.id, alert_type=AlertType.DROP))
        trigger = _make_trigger(alert_id=alert.id, route_id=route.id)

        mock_ap = MagicMock()
        mock_apprise_mod.Apprise.return_value = mock_ap
        mock_ap.notify.return_value = delivery_ok

        exception_text = ""
        with caplog.at_level("DEBUG"):
            if delivery_ok:
                send_digest([trigger], db)
            else:
                with pytest.raises(NotificationDeliveryError) as exc_info:
                    send_digest([trigger], db)
                exception_text = str(exc_info.value)
        db.close()

        captured = capsys.readouterr()
        assert self.SECRET not in captured.out
        assert self.SECRET not in captured.err
        assert self.SECRET not in caplog.text
        assert self.SECRET not in exception_text
        assert self.SECRET.encode() not in db_path.read_bytes()

    @patch("fli.tracker.notifier.apprise")
    @patch("fli.tracker.notifier._HAS_APPRISE", True)
    def test_config_error_message_carries_no_secret(
        self, mock_apprise_mod, monkeypatch, db: TrackerDB
    ):
        """The fail-closed exception text names the variable, not a value."""
        monkeypatch.delenv("NOTIFY_URL", raising=False)
        with pytest.raises(NotificationConfigError) as exc_info:
            send_digest([_make_trigger()], db)
        assert "NOTIFY_URL is not configured" in str(exc_info.value)


class TestIsDomesticRoute:
    """Tests for domestic/international route classification."""

    @pytest.mark.parametrize(
        ("origin", "destination", "expected"),
        [
            pytest.param("DFW", "ORD", True, id="us_to_us"),
            pytest.param("DFW", "SJU", True, id="us_to_puerto_rico"),
            pytest.param("SJU", "JFK", True, id="puerto_rico_to_us"),
            pytest.param("DFW", "FCO", False, id="us_to_italy"),
            pytest.param("DFW", "CUN", False, id="us_to_mexico"),
            pytest.param("LHR", "CDG", False, id="uk_to_france"),
            pytest.param("ZZZ", "DFW", False, id="unknown_origin"),
            pytest.param("DFW", "ZZZ", False, id="unknown_destination"),
        ],
    )
    def test_classification(self, origin, destination, expected):
        assert _is_domestic_route(origin, destination) is expected


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

    def test_countries_dict_loaded(self):
        """Airport countries dict is populated alongside locations."""
        from fli.tracker.notifier import _AIRPORT_COUNTRIES

        assert _AIRPORT_COUNTRIES["DFW"] == "US"
        assert _AIRPORT_COUNTRIES["SJU"] == "Puerto Rico"
        assert _AIRPORT_COUNTRIES["FCO"] == "Italy"
        assert _AIRPORT_COUNTRIES["NRT"] == "Japan"
        assert _AIRPORT_COUNTRIES["CUN"] == "Mexico"

    def test_tracked_airports_have_metadata(self):
        """Every airport in the current tracked routes has location metadata."""
        from fli.tracker.notifier import _AIRPORT_LOCATIONS

        tracked_codes = [
            "DFW",
            "ATL",
            "ORD",
            "LAX",
            "SFO",
            "SEA",
            "MIA",
            "BOS",
            "JFK",
            "DEN",
            "MSP",
            "PHX",
            "HNL",
            "ANC",
            "PWM",
            "CUN",
            "SJO",
            "LHR",
            "CDG",
            "FCO",
            "BCN",
            "LIS",
            "AMS",
            "DUB",
            "ATH",
            "IST",
            "NRT",
            "ICN",
            "BKK",
            "SYD",
            "AKL",
            "GRU",
            "EZE",
            "BOG",
            "SCL",
        ]
        missing = [c for c in tracked_codes if c not in _AIRPORT_LOCATIONS]
        assert missing == [], f"Missing airport metadata for: {missing}"

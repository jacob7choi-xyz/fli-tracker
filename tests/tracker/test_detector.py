"""Tests for the detector module.

Verifies drop detection and threshold detection logic, including
edge cases like first scan (no history), exact threshold match,
and notification deduplication.
"""

from pathlib import Path

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.detector import _min_improvement, check_alerts
from fli.tracker.models import Alert, AlertType, NotificationRecord, PriceSnapshot, Route


@pytest.fixture()
def db(tmp_path: Path) -> TrackerDB:
    return TrackerDB(db_path=tmp_path / "test.db")


@pytest.fixture()
def route_with_alert(db: TrackerDB):
    """Create a route with a drop alert and return (db, route, alert)."""
    route = db.add_route(Route(origin="DFW", destination="FCO"))
    alert = db.add_alert(
        Alert(
            route_id=route.id,
            alert_type=AlertType.DROP,
            notify_url="test://url",
        )
    )
    return db, route, alert


def _snap(route_id: int, price: float, departure_date: str = "2026-07-15") -> PriceSnapshot:
    return PriceSnapshot(
        route_id=route_id,
        departure_date=departure_date,
        price=price,
        currency="USD",
    )


# ------------------------------------------------------------------
# Drop alerts
# ------------------------------------------------------------------


class TestDropAlerts:
    """Tests for all-time low detection."""

    def test_no_history_does_not_trigger(self, route_with_alert):
        """First scan has no previous low, so drop alert should not fire."""
        db, route, alert = route_with_alert
        snapshots = [_snap(route.id, 500.0)]

        triggers = check_alerts(db, route, snapshots)

        assert len(triggers) == 0

    def test_new_low_triggers(self, route_with_alert):
        """Price below historical minimum should trigger."""
        db, route, alert = route_with_alert

        # Seed historical data
        db.add_snapshots([_snap(route.id, 500.0)])

        # New lower price
        triggers = check_alerts(db, route, [_snap(route.id, 450.0)])

        assert len(triggers) == 1
        assert triggers[0].alert.id == alert.id
        assert triggers[0].previous_low == 500.0
        assert triggers[0].snapshot.price == 450.0

    def test_equal_price_does_not_trigger(self, route_with_alert):
        """Same price as minimum should not trigger (must be strictly less)."""
        db, route, alert = route_with_alert
        db.add_snapshots([_snap(route.id, 500.0)])

        triggers = check_alerts(db, route, [_snap(route.id, 500.0)])

        assert len(triggers) == 0

    def test_higher_price_does_not_trigger(self, route_with_alert):
        db, route, alert = route_with_alert
        db.add_snapshots([_snap(route.id, 500.0)])

        triggers = check_alerts(db, route, [_snap(route.id, 550.0)])

        assert len(triggers) == 0

    def test_drop_per_departure_date(self, route_with_alert):
        """Each departure date has its own price history."""
        db, route, alert = route_with_alert

        db.add_snapshots(
            [
                _snap(route.id, 500.0, "2026-07-15"),
                _snap(route.id, 300.0, "2026-07-20"),
            ]
        )

        # Price drop only on 07-15, not on 07-20
        triggers = check_alerts(
            db,
            route,
            [
                _snap(route.id, 450.0, "2026-07-15"),
                _snap(route.id, 350.0, "2026-07-20"),
            ],
        )

        assert len(triggers) == 1
        assert triggers[0].snapshot.departure_date == "2026-07-15"

    def test_trigger_includes_route_context(self, route_with_alert):
        db, route, alert = route_with_alert
        db.add_snapshots([_snap(route.id, 500.0)])

        triggers = check_alerts(db, route, [_snap(route.id, 400.0)])

        assert triggers[0].route.origin == "DFW"
        assert triggers[0].route.destination == "FCO"


# ------------------------------------------------------------------
# Threshold alerts
# ------------------------------------------------------------------


class TestThresholdAlerts:
    """Tests for price threshold detection."""

    def test_below_threshold_triggers(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 450.0)])

        assert len(triggers) == 1
        assert triggers[0].snapshot.price == 450.0

    def test_exact_threshold_triggers(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 500.0)])

        assert len(triggers) == 1

    def test_above_threshold_does_not_trigger(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 550.0)])

        assert len(triggers) == 0

    def test_dedup_prevents_repeat(self, db: TrackerDB):
        """Same (alert, departure_date, price) should not trigger twice."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        # Simulate a previous notification with structural dedup fields
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                departure_date="2026-07-15",
                price=450.0,
                message="test",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 450.0)])

        assert len(triggers) == 0

    def test_high_price_small_drop_suppressed(self, db: TrackerDB):
        """$55+ flight: small improvement (< $60) suppressed after first notification."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        # Previous notification at $450
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                departure_date="2026-07-15",
                price=450.0,
                message="test",
            )
        )

        # $448 -- only $2 improvement, suppressed
        triggers = check_alerts(db, route, [_snap(route.id, 448.0)])

        assert len(triggers) == 0

    def test_high_price_big_drop_re_alerts(self, db: TrackerDB):
        """$55+ flight: large improvement ($60+) re-alerts even after first notification."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        # Previous notification at $450
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                departure_date="2026-07-15",
                price=450.0,
                message="test",
            )
        )

        # $380 -- $70 improvement (>= $30), should fire
        triggers = check_alerts(db, route, [_snap(route.id, 380.0)])

        assert len(triggers) == 1

    def test_low_price_re_alerts_on_any_drop(self, db: TrackerDB):
        """Sub-$55 flight: re-alerts even on a $1 improvement from last notified."""
        route = db.add_route(Route(origin="DFW", destination="MCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=60.0,
                notify_url="test://url",
            )
        )

        # Previous notification at $49
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                departure_date="2026-07-15",
                price=49.0,
                message="test",
            )
        )

        # New scan at $48 -- sub-$55 and $1 cheaper, should fire
        triggers = check_alerts(db, route, [_snap(route.id, 48.0)])

        assert len(triggers) == 1

    def test_low_price_same_price_suppressed(self, db: TrackerDB):
        """Sub-$55 flight: no re-alert when price hasn't improved."""
        route = db.add_route(Route(origin="DFW", destination="MCO"))
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=60.0,
                notify_url="test://url",
            )
        )

        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                departure_date="2026-07-15",
                price=49.0,
                message="test",
            )
        )

        # Same price -- no improvement, no re-alert
        triggers = check_alerts(db, route, [_snap(route.id, 49.0)])

        assert len(triggers) == 0

    def test_threshold_none_does_not_trigger(self, db: TrackerDB):
        """Threshold alert with threshold=None should not trigger."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=None,
                notify_url="test://url",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 100.0)])

        assert len(triggers) == 0


# ------------------------------------------------------------------
# _min_improvement thresholds
# ------------------------------------------------------------------


class TestMinImprovement:
    """Tests for the tiered hybrid re-alert threshold function."""

    @pytest.mark.parametrize(
        "last_notified, expected",
        [
            (55.0, 20.0),  # $55-$199 band: max($20, 10%) = $20 (10% = $5.5)
            (100.0, 20.0),  # max($20, 10%) = $20 (10% = $10)
            (180.0, 20.0),  # max($20, 10%) = $20 (10% = $18)
            (200.0, 30.0),  # $200-$599 band: max($30, 8%) = $30 (8% = $16)
            (400.0, 32.0),  # max($30, 8%) = $32 (8% of $400)
            (450.0, 36.0),  # max($30, 8%) = $36 (8% of $450)
            (599.0, 47.92),  # max($30, 8%) = $47.92 (8% of $599)
            (600.0, 50.0),  # $600+ band: max($50, 6%) = $50 (6% = $36)
            (900.0, 54.0),  # max($50, 6%) = $54 (6% of $900)
            (1000.0, 60.0),  # max($50, 6%) = $60 (6% of $1000)
        ],
    )
    def test_threshold_by_band(self, last_notified: float, expected: float):
        assert abs(_min_improvement(last_notified) - expected) < 0.01


# ------------------------------------------------------------------
# Re-alert suppression
# ------------------------------------------------------------------


class TestReAlertSuppression:
    """Tests for the $55 re-alert suppression rule.

    $55+ flights: suppress all re-alerts once a notification has been sent.
    Sub-$55 flights: re-alert on any further price drop, even $1.
    """

    def test_drop_high_price_small_improvement_suppressed(self, db: TrackerDB):
        """$55+ DROP alert: small improvement (< $60) suppressed after first notification."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(route_id=route.id, alert_type=AlertType.DROP, notify_url="test://url")
        )

        # Seed history and simulate a prior notification at $450
        db.add_snapshots([_snap(route.id, 500.0)])
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id, departure_date="2026-07-15", price=450.0, message="test"
            )
        )
        db.add_snapshots([_snap(route.id, 450.0)])

        # $448 -- only $2 improvement, suppressed
        triggers = check_alerts(db, route, [_snap(route.id, 448.0)])

        assert len(triggers) == 0

    def test_drop_high_price_big_improvement_re_alerts(self, db: TrackerDB):
        """$55+ DROP alert: large improvement ($60+) re-alerts after first notification."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        alert = db.add_alert(
            Alert(route_id=route.id, alert_type=AlertType.DROP, notify_url="test://url")
        )

        db.add_snapshots([_snap(route.id, 500.0)])
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id, departure_date="2026-07-15", price=450.0, message="test"
            )
        )
        db.add_snapshots([_snap(route.id, 450.0)])

        # $380 -- $70 improvement (>= $30), should fire
        triggers = check_alerts(db, route, [_snap(route.id, 380.0)])

        assert len(triggers) == 1

    def test_drop_low_price_re_alerts_on_any_drop(self, db: TrackerDB):
        """Sub-$55 DROP alert: re-alerts even on a $1 improvement."""
        route = db.add_route(Route(origin="DFW", destination="MCO"))
        alert = db.add_alert(
            Alert(route_id=route.id, alert_type=AlertType.DROP, notify_url="test://url")
        )

        db.add_snapshots([_snap(route.id, 52.0)])
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id, departure_date="2026-07-15", price=49.0, message="test"
            )
        )
        db.add_snapshots([_snap(route.id, 49.0)])

        # $48 -- sub-$55 and $1 cheaper than last notified
        triggers = check_alerts(db, route, [_snap(route.id, 48.0)])

        assert len(triggers) == 1

    def test_drop_first_notification_always_fires(self, route_with_alert):
        """First notification (no prior record) always fires regardless of price."""
        db, route, alert = route_with_alert
        db.add_snapshots([_snap(route.id, 500.0)])

        # No prior notification -- should fire even though $55+
        triggers = check_alerts(db, route, [_snap(route.id, 450.0)])

        assert len(triggers) == 1

    def test_threshold_first_notification_always_fires(self, db: TrackerDB):
        """First threshold notification (no prior record) always fires."""
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 450.0)])

        assert len(triggers) == 1


# ------------------------------------------------------------------
# No alerts
# ------------------------------------------------------------------


class TestNoAlerts:
    """Tests for routes with no alerts configured."""

    def test_no_alerts_returns_empty(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        triggers = check_alerts(db, route, [_snap(route.id, 500.0)])
        assert triggers == []


# ------------------------------------------------------------------
# Mixed alerts
# ------------------------------------------------------------------


class TestMixedAlerts:
    """Tests for routes with both drop and threshold alerts."""

    def test_both_types_can_fire(self, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://drop",
            )
        )
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://threshold",
            )
        )

        # Seed history so drop can fire
        db.add_snapshots([_snap(route.id, 500.0)])

        # New price below both the historical low and the threshold
        triggers = check_alerts(db, route, [_snap(route.id, 400.0)])

        assert len(triggers) == 2
        types = {t.alert.alert_type for t in triggers}
        assert AlertType.DROP in types
        assert AlertType.THRESHOLD in types

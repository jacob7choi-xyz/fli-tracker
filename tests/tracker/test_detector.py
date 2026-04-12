"""Tests for the detector module.

Verifies drop detection and threshold detection logic, including
edge cases like first scan (no history), exact threshold match,
and notification deduplication.
"""

from pathlib import Path

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.detector import check_alerts
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

        # Simulate a previous notification
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                price=450.0,
                message="Price drop: DFW -> FCO, departure 2026-07-15",
            )
        )

        triggers = check_alerts(db, route, [_snap(route.id, 450.0)])

        assert len(triggers) == 0

    def test_different_price_still_triggers(self, db: TrackerDB):
        """A new lower price should trigger even if a prior notification exists."""
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
                price=450.0,
                message="Price drop: DFW -> FCO, departure 2026-07-15",
            )
        )

        # New scan at $400
        triggers = check_alerts(db, route, [_snap(route.id, 400.0)])

        assert len(triggers) == 1

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

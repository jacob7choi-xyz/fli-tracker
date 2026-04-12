from pathlib import Path

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.models import Alert, AlertType, NotificationRecord, PriceSnapshot, Route


@pytest.fixture()
def db(tmp_path: Path) -> TrackerDB:
    """Create a TrackerDB backed by a temporary file."""
    return TrackerDB(db_path=tmp_path / "test_tracker.db")


@pytest.fixture()
def sample_route() -> Route:
    return Route(origin="DFW", destination="FCO")


@pytest.fixture()
def db_with_route(db: TrackerDB, sample_route: Route) -> tuple[TrackerDB, Route]:
    """Return a DB and a persisted route."""
    route = db.add_route(sample_route)
    return db, route


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------


class TestSchema:
    """Tests for database initialization and schema."""

    def test_creates_db_file(self, tmp_path: Path):
        db_path = tmp_path / "sub" / "dir" / "tracker.db"
        db = TrackerDB(db_path=db_path)
        assert db_path.exists()
        db.close()

    def test_tables_exist(self, db: TrackerDB):
        tables = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = sorted(r["name"] for r in tables)
        assert "alerts" in table_names
        assert "notification_log" in table_names
        assert "price_snapshots" in table_names
        assert "routes" in table_names

    def test_foreign_keys_enabled(self, db: TrackerDB):
        row = db._conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_idempotent_schema_init(self, tmp_path: Path):
        """Opening the same DB twice does not fail."""
        db_path = tmp_path / "tracker.db"
        db1 = TrackerDB(db_path=db_path)
        db1.close()
        db2 = TrackerDB(db_path=db_path)
        db2.close()


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------


class TestRoutes:
    """Tests for route CRUD operations."""

    def test_add_route_assigns_id(self, db: TrackerDB, sample_route: Route):
        route = db.add_route(sample_route)
        assert route.id is not None
        assert route.id > 0

    def test_get_route_returns_matching(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        fetched = db.get_route(route.id)
        assert fetched is not None
        assert fetched.origin == "DFW"
        assert fetched.destination == "FCO"
        assert fetched.cabin_class == "ECONOMY"
        assert fetched.is_round_trip is True

    def test_get_route_returns_none_for_missing(self, db: TrackerDB):
        assert db.get_route(999) is None

    def test_list_routes_active_only(self, db: TrackerDB):
        db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_route(Route(origin="JFK", destination="LHR", active=False))
        active = db.list_routes(active_only=True)
        assert len(active) == 1
        assert active[0].origin == "DFW"

    def test_list_routes_all(self, db: TrackerDB):
        db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_route(Route(origin="JFK", destination="LHR", active=False))
        all_routes = db.list_routes(active_only=False)
        assert len(all_routes) == 2

    def test_set_route_active(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        assert db.set_route_active(route.id, False) is True
        fetched = db.get_route(route.id)
        assert fetched.active is False
        assert db.set_route_active(route.id, True) is True
        fetched = db.get_route(route.id)
        assert fetched.active is True

    def test_set_route_active_nonexistent(self, db: TrackerDB):
        assert db.set_route_active(999, True) is False

    def test_remove_route(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        assert db.remove_route(route.id) is True
        assert db.get_route(route.id) is None

    def test_remove_route_nonexistent(self, db: TrackerDB):
        assert db.remove_route(999) is False

    def test_remove_route_cascades(self, db_with_route: tuple[TrackerDB, Route]):
        """Removing a route also removes its snapshots, alerts, and notifications."""
        db, route = db_with_route

        # Add a snapshot
        db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
                )
            ]
        )

        # Add an alert
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=400.0,
                notify_url="test://url",
            )
        )

        # Add a notification
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                price=350.0,
                message="test",
            )
        )

        # Remove route
        assert db.remove_route(route.id) is True

        # Verify cascade
        assert db.get_snapshots(route.id) == []
        assert db.list_alerts(route_id=route.id) == []


# ------------------------------------------------------------------
# Price snapshots
# ------------------------------------------------------------------


class TestPriceSnapshots:
    """Tests for price snapshot storage and retrieval."""

    def test_add_and_get_snapshots(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        snapshots = [
            PriceSnapshot(
                route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
            ),
            PriceSnapshot(
                route_id=route.id, departure_date="2026-07-16", price=450.0, currency="USD"
            ),
        ]
        count = db.add_snapshots(snapshots)
        assert count == 2

        fetched = db.get_snapshots(route.id)
        assert len(fetched) == 2

    def test_get_snapshots_filtered_by_date(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-16", price=450.0, currency="USD"
                ),
            ]
        )
        fetched = db.get_snapshots(route.id, departure_date="2026-07-15")
        assert len(fetched) == 1
        assert fetched[0].departure_date == "2026-07-15"

    def test_get_snapshots_with_limit(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-16", price=450.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-17", price=400.0, currency="USD"
                ),
            ]
        )
        fetched = db.get_snapshots(route.id, limit=2)
        assert len(fetched) == 2

    def test_add_empty_list(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        assert db.add_snapshots([]) == 0

    def test_get_min_price(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=450.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=480.0, currency="USD"
                ),
            ]
        )
        assert db.get_min_price(route.id, "2026-07-15") == 450.0

    def test_get_min_price_no_data(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        assert db.get_min_price(route.id, "2026-07-15") is None

    def test_snapshots_ordered_newest_first(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        # Insert with explicit scanned_at to control ordering
        insert_sql = (
            "INSERT INTO price_snapshots"
            " (route_id, departure_date, price, currency, scanned_at)"
            " VALUES (?, ?, ?, ?, ?)"
        )
        db._conn.execute(
            insert_sql,
            (route.id, "2026-07-15", 500.0, "USD", "2026-01-01 00:00:00"),
        )
        db._conn.execute(
            insert_sql,
            (route.id, "2026-07-15", 450.0, "USD", "2026-01-02 00:00:00"),
        )
        db._conn.commit()
        fetched = db.get_snapshots(route.id)
        assert fetched[0].price == 450.0
        assert fetched[1].price == 500.0


# ------------------------------------------------------------------
# Alerts
# ------------------------------------------------------------------


class TestAlerts:
    """Tests for alert CRUD operations."""

    def test_add_alert_assigns_id(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="tgram://bot/chat",
            )
        )
        assert alert.id is not None
        assert alert.id > 0

    def test_list_alerts_by_route(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        alerts = db.list_alerts(route_id=route.id)
        assert len(alerts) == 1
        assert alerts[0].alert_type == AlertType.DROP

    def test_list_alerts_active_only(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
                active=True,
            )
        )
        db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=300.0,
                notify_url="test://url2",
                active=False,
            )
        )
        active = db.list_alerts(route_id=route.id, active_only=True)
        assert len(active) == 1
        all_alerts = db.list_alerts(route_id=route.id, active_only=False)
        assert len(all_alerts) == 2

    def test_remove_alert(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        assert db.remove_alert(alert.id) is True
        assert db.list_alerts(route_id=route.id) == []

    def test_remove_alert_nonexistent(self, db: TrackerDB):
        assert db.remove_alert(999) is False


# ------------------------------------------------------------------
# Notification log
# ------------------------------------------------------------------


class TestNotificationLog:
    """Tests for notification logging and deduplication."""

    def test_log_notification(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        record = db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                price=487.0,
                message="Price drop: DFW -> FCO, departure 2026-07-15",
            )
        )
        assert record.id is not None

    def test_was_notification_sent_true(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                price=487.0,
                message="Price drop: DFW -> FCO, departure 2026-07-15",
            )
        )
        assert db.was_notification_sent(alert.id, "2026-07-15", 487.0) is True

    def test_was_notification_sent_false_different_price(
        self, db_with_route: tuple[TrackerDB, Route]
    ):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                price=487.0,
                message="Price drop: DFW -> FCO, departure 2026-07-15",
            )
        )
        assert db.was_notification_sent(alert.id, "2026-07-15", 400.0) is False

    def test_was_notification_sent_false_no_records(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        assert db.was_notification_sent(alert.id, "2026-07-15", 487.0) is False

    def test_remove_alert_cascades_notifications(self, db_with_route: tuple[TrackerDB, Route]):
        db, route = db_with_route
        alert = db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                price=487.0,
                message="test notification",
            )
        )
        db.remove_alert(alert.id)
        assert db.was_notification_sent(alert.id, "test", 487.0) is False

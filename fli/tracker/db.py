"""SQLite storage layer for the tracker.

Manages the database schema, route CRUD, price snapshot storage,
alert configuration, and notification logging.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from fli.tracker.models import Alert, AlertType, NotificationRecord, PriceSnapshot, Route

DEFAULT_DB_PATH = Path(os.environ.get("FLI_DB_PATH", Path.home() / ".fli" / "tracker.db"))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routes (
    id            INTEGER PRIMARY KEY,
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    cabin_class   TEXT NOT NULL DEFAULT 'ECONOMY',
    max_stops     TEXT NOT NULL DEFAULT 'ANY',
    trip_duration INTEGER NOT NULL DEFAULT 7,
    look_ahead    INTEGER NOT NULL DEFAULT 90,
    is_round_trip INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id             INTEGER PRIMARY KEY,
    route_id       INTEGER NOT NULL REFERENCES routes(id),
    departure_date TEXT NOT NULL,
    return_date    TEXT,
    price          REAL NOT NULL,
    currency       TEXT NOT NULL,
    scanned_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_route_date
    ON price_snapshots(route_id, departure_date);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY,
    route_id   INTEGER NOT NULL REFERENCES routes(id),
    alert_type TEXT NOT NULL,
    threshold  REAL,
    notify_url TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notification_log (
    id       INTEGER PRIMARY KEY,
    alert_id INTEGER NOT NULL REFERENCES alerts(id),
    price    REAL NOT NULL,
    message  TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class TrackerDB:
    """SQLite database interface for the tracker.

    Handles schema creation, route management, snapshot storage,
    alert configuration, and notification logging.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        """Open or create the tracker database.

        Args:
            db_path: Path to the SQLite file. Parent directories are
                created automatically if they do not exist.

        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def add_route(self, route: Route) -> Route:
        """Insert a new route and return it with its assigned id."""
        cur = self._conn.execute(
            """
            INSERT INTO routes (origin, destination, cabin_class, max_stops,
                                trip_duration, look_ahead, is_round_trip, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route.origin,
                route.destination,
                route.cabin_class,
                route.max_stops,
                route.trip_duration,
                route.look_ahead,
                int(route.is_round_trip),
                int(route.active),
            ),
        )
        self._conn.commit()
        route.id = cur.lastrowid
        return route

    def get_route(self, route_id: int) -> Route | None:
        """Fetch a single route by id, or None if not found."""
        row = self._conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_route(row)

    def list_routes(self, active_only: bool = True) -> list[Route]:
        """Return all routes, optionally filtering to active ones."""
        if active_only:
            rows = self._conn.execute("SELECT * FROM routes WHERE active = 1").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM routes").fetchall()
        return [self._row_to_route(r) for r in rows]

    def set_route_active(self, route_id: int, active: bool) -> bool:
        """Activate or deactivate a route. Returns True if the row existed."""
        cur = self._conn.execute(
            "UPDATE routes SET active = ? WHERE id = ?",
            (int(active), route_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def remove_route(self, route_id: int) -> bool:
        """Delete a route and its associated snapshots, alerts, and notifications.

        Returns True if the route existed.
        """
        # Delete notification log entries for this route's alerts
        self._conn.execute(
            """
            DELETE FROM notification_log
            WHERE alert_id IN (SELECT id FROM alerts WHERE route_id = ?)
            """,
            (route_id,),
        )
        self._conn.execute("DELETE FROM alerts WHERE route_id = ?", (route_id,))
        self._conn.execute("DELETE FROM price_snapshots WHERE route_id = ?", (route_id,))
        cur = self._conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_route(row: sqlite3.Row) -> Route:
        return Route(
            id=row["id"],
            origin=row["origin"],
            destination=row["destination"],
            cabin_class=row["cabin_class"],
            max_stops=row["max_stops"],
            trip_duration=row["trip_duration"],
            look_ahead=row["look_ahead"],
            is_round_trip=bool(row["is_round_trip"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            active=bool(row["active"]),
        )

    # ------------------------------------------------------------------
    # Price snapshots
    # ------------------------------------------------------------------

    def add_snapshots(self, snapshots: list[PriceSnapshot]) -> int:
        """Bulk-insert price snapshots. Returns number of rows inserted."""
        if not snapshots:
            return 0
        self._conn.executemany(
            """
            INSERT INTO price_snapshots (route_id, departure_date, return_date,
                                         price, currency)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(s.route_id, s.departure_date, s.return_date, s.price, s.currency) for s in snapshots],
        )
        self._conn.commit()
        return len(snapshots)

    def get_snapshots(
        self,
        route_id: int,
        departure_date: str | None = None,
        limit: int | None = None,
    ) -> list[PriceSnapshot]:
        """Fetch price snapshots for a route, optionally filtered by departure date.

        Results are ordered by scanned_at descending (newest first).
        """
        query = "SELECT * FROM price_snapshots WHERE route_id = ?"
        params: list = [route_id]

        if departure_date is not None:
            query += " AND departure_date = ?"
            params.append(departure_date)

        query += " ORDER BY scanned_at DESC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_min_price(self, route_id: int, departure_date: str) -> float | None:
        """Return the historical minimum price for a route and departure date.

        Returns None if no snapshots exist.
        """
        row = self._conn.execute(
            """
            SELECT MIN(price) AS min_price FROM price_snapshots
            WHERE route_id = ? AND departure_date = ?
            """,
            (route_id, departure_date),
        ).fetchone()
        if row is None or row["min_price"] is None:
            return None
        return float(row["min_price"])

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> PriceSnapshot:
        return PriceSnapshot(
            id=row["id"],
            route_id=row["route_id"],
            departure_date=row["departure_date"],
            return_date=row["return_date"],
            price=row["price"],
            currency=row["currency"],
            scanned_at=datetime.fromisoformat(row["scanned_at"]),
        )

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def add_alert(self, alert: Alert) -> Alert:
        """Insert a new alert and return it with its assigned id."""
        cur = self._conn.execute(
            """
            INSERT INTO alerts (route_id, alert_type, threshold, notify_url, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                alert.route_id,
                alert.alert_type.value,
                alert.threshold,
                alert.notify_url,
                int(alert.active),
            ),
        )
        self._conn.commit()
        alert.id = cur.lastrowid
        return alert

    def list_alerts(self, route_id: int | None = None, active_only: bool = True) -> list[Alert]:
        """Return alerts, optionally filtered by route and active status."""
        query = "SELECT * FROM alerts WHERE 1=1"
        params: list = []

        if route_id is not None:
            query += " AND route_id = ?"
            params.append(route_id)
        if active_only:
            query += " AND active = 1"

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def remove_alert(self, alert_id: int) -> bool:
        """Delete an alert and its notification log entries. Returns True if it existed."""
        self._conn.execute("DELETE FROM notification_log WHERE alert_id = ?", (alert_id,))
        cur = self._conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> Alert:
        return Alert(
            id=row["id"],
            route_id=row["route_id"],
            alert_type=AlertType(row["alert_type"]),
            threshold=row["threshold"],
            notify_url=row["notify_url"],
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Notification log
    # ------------------------------------------------------------------

    def log_notification(self, record: NotificationRecord) -> NotificationRecord:
        """Insert a notification log entry and return it with its assigned id."""
        cur = self._conn.execute(
            "INSERT INTO notification_log (alert_id, price, message) VALUES (?, ?, ?)",
            (record.alert_id, record.price, record.message),
        )
        self._conn.commit()
        record.id = cur.lastrowid
        return record

    def was_notification_sent(self, alert_id: int, departure_date: str, price: float) -> bool:
        """Check if a notification was already sent for this alert, date, and price.

        Used to deduplicate threshold alerts so users are not spammed
        for the same price on the same departure date.
        """
        row = self._conn.execute(
            """
            SELECT 1 FROM notification_log
            WHERE alert_id = ? AND message LIKE ? AND price = ?
            LIMIT 1
            """,
            (alert_id, f"%{departure_date}%", price),
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_notification(row: sqlite3.Row) -> NotificationRecord:
        return NotificationRecord(
            id=row["id"],
            alert_id=row["alert_id"],
            price=row["price"],
            message=row["message"],
            sent_at=datetime.fromisoformat(row["sent_at"]),
        )

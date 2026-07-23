"""Tests for scripts/prune_tracker_db.py.

The pruning invariants under test:
- Suppression rows for future trips MUST survive.
- Expired trips (departure more than one day past, UTC) are deleted; the
  one-day grace prefers false retention over false deletion at calendar
  boundaries.
- NULL departure_date rows are provably dead for dedup (SQL equality never
  matches NULL) and are all deleted.
- NULL return_date rows are LIVE one-way trips, never confused with dead rows.
- Snapshot retention behavior is unchanged by this incident.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.models import Alert, AlertType, NotificationRecord, PriceSnapshot, Route

_SPEC = importlib.util.spec_from_file_location(
    "prune_tracker_db",
    Path(__file__).resolve().parents[2] / "scripts" / "prune_tracker_db.py",
)
prune_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prune_mod)


@pytest.fixture()
def db(tmp_path: Path) -> TrackerDB:
    return TrackerDB(db_path=tmp_path / "test.db")


@pytest.fixture()
def seeded(db: TrackerDB):
    """Provide a route with an alert, plus a helper to log notification rows."""
    route = db.add_route(Route(origin="DFW", destination="FCO"))
    alert = db.add_alert(Alert(route_id=route.id, alert_type=AlertType.DROP))

    def log(departure_date: str | None, return_date: str | None = None, price: float = 400.0):
        db.log_notification(
            NotificationRecord(
                alert_id=alert.id,
                departure_date=departure_date,
                return_date=return_date,
                price=price,
                message="digest",
            )
        )

    return db, alert, log


def _sqlite_date(db: TrackerDB, offset_days: int) -> str:
    """Compute a date on SQLite's own UTC clock, matching the DELETE's clock."""
    return db._conn.execute("SELECT date('now', ?)", (f"{offset_days} days",)).fetchone()[0]


class TestPruneNotificationLog:
    @pytest.mark.parametrize(
        ("offset_days", "survives"),
        [
            (-30, False),  # long expired
            (-2, False),  # expired, outside grace
            (-1, True),  # grace day: uncertain boundary -> preserve
            (0, True),  # departs today
            (14, True),  # future trip
            (90, True),  # far future trip
        ],
    )
    def test_expiry_boundary(self, seeded, offset_days, survives):
        db, alert, log = seeded
        dep = _sqlite_date(db, offset_days)
        log(dep, return_date=None)

        prune_mod.prune_notification_log(db._conn)

        remaining = db._conn.execute(
            "SELECT COUNT(*) FROM notification_log WHERE departure_date = ?", (dep,)
        ).fetchone()[0]
        assert (remaining == 1) is survives

    def test_null_departure_rows_all_deleted(self, seeded):
        db, alert, log = seeded
        log(None)
        log(None, price=350.0)

        deleted = prune_mod.prune_notification_log(db._conn)

        assert deleted == 2
        count = db._conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        assert count == 0

    def test_null_departure_deletion_cannot_change_dedup(self, seeded):
        """Encode the NULL-deadness proof as a test.

        Dedup filters departure_date = ?, and SQL equality never matches
        NULL, so NULL rows influence no dedup answer.
        """
        db, alert, log = seeded
        future = _sqlite_date(db, 30)
        log(None, price=400.0)

        # A NULL row never answers a dedup query, even for its own price
        assert db.was_notification_sent(alert.id, future, 400.0) is False
        assert db.get_last_notified_price(alert.id, future) is None

        prune_mod.prune_notification_log(db._conn)

        assert db.was_notification_sent(alert.id, future, 400.0) is False
        assert db.get_last_notified_price(alert.id, future) is None

    def test_one_way_future_rows_survive(self, seeded):
        """NULL return_date is a live one-way trip, not a dead row."""
        db, alert, log = seeded
        future = _sqlite_date(db, 21)
        log(future, return_date=None, price=220.0)

        prune_mod.prune_notification_log(db._conn)

        assert db.was_notification_sent(alert.id, future, 220.0) is True
        assert db.get_last_notified_price(alert.id, future) == 220.0

    def test_dedup_answers_identical_for_survivors(self, seeded):
        """Verify consumer-level before/after equivalence for surviving keys.

        Includes multi-price sets: exact-price membership and min-per-key
        must both be preserved.
        """
        db, alert, log = seeded
        future = _sqlite_date(db, 45)
        ret = _sqlite_date(db, 52)
        for price in (280.0, 300.0, 310.0):
            log(future, return_date=ret, price=price)
        log(_sqlite_date(db, -10), return_date=None, price=99.0)  # expired noise
        log(None, price=99.0)  # dead NULL noise

        before = {
            "sent_280": db.was_notification_sent(alert.id, future, 280.0, ret),
            "sent_300": db.was_notification_sent(alert.id, future, 300.0, ret),
            "sent_290": db.was_notification_sent(alert.id, future, 290.0, ret),
            "min": db.get_last_notified_price(alert.id, future, ret),
        }

        prune_mod.prune_notification_log(db._conn)

        after = {
            "sent_280": db.was_notification_sent(alert.id, future, 280.0, ret),
            "sent_300": db.was_notification_sent(alert.id, future, 300.0, ret),
            "sent_290": db.was_notification_sent(alert.id, future, 290.0, ret),
            "min": db.get_last_notified_price(alert.id, future, ret),
        }

        assert before == after
        assert before["sent_280"] is True
        assert before["sent_290"] is False
        assert before["min"] == 280.0


class TestPruneSnapshots:
    @pytest.mark.parametrize(
        ("age_days", "survives"),
        [(30, False), (20, False), (5, True), (0, True)],
    )
    def test_retention_window(self, db: TrackerDB, age_days, survives):
        route = db.add_route(Route(origin="DFW", destination="ORD"))
        db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id,
                    departure_date="2099-01-01",
                    price=100.0,
                    currency="USD",
                )
            ]
        )
        # Backdate via SQL using the same localtime clock the prune uses
        db._conn.execute(
            "UPDATE price_snapshots SET scanned_at = datetime('now', 'localtime', ?)",
            (f"-{age_days} days",),
        )
        db._conn.commit()

        prune_mod.prune_snapshots(db._conn, retention_days=14)

        count = db._conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
        assert (count == 1) is survives


class TestMaybeVacuum:
    def _bloated_db(self, tmp_path: Path) -> str:
        path = str(tmp_path / "bloat.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [("y" * 1000,)] * 500)
        conn.commit()
        conn.execute("DELETE FROM t")
        conn.commit()
        conn.close()
        return path

    def test_vacuum_runs_above_threshold(self, tmp_path: Path):
        path = self._bloated_db(tmp_path)
        conn = sqlite3.connect(path)
        ratio, vacuumed = prune_mod.maybe_vacuum(conn, threshold=0.10)
        conn.close()
        assert ratio > 0.10
        assert vacuumed is True

    def test_vacuum_skipped_below_threshold(self, tmp_path: Path):
        path = self._bloated_db(tmp_path)
        conn = sqlite3.connect(path)
        ratio, vacuumed = prune_mod.maybe_vacuum(conn, threshold=0.999)
        conn.close()
        assert vacuumed is False

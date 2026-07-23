"""Prune old tracker data and optionally VACUUM the database.

Prunes two tables with deliberately different semantics:

- price_snapshots: rolling retention window in days. Uses 'localtime',
  a deliberate codebase convention matching Python date.today() elsewhere
  (see commit 45139e5). Do not change to UTC here without unifying the
  timezone convention everywhere (tracked follow-up).
- notification_log: suppression rows for future trips must survive; rows
  are removed only when provably dead for the dedup consumers.

Reads configuration from environment variables when run as a script:
    FLI_DB_PATH          -- path to tracker.db (required)
    FLI_RETENTION_DAYS   -- days of snapshots to keep (default: 14)
    FLI_VACUUM_THRESHOLD -- freelist/page ratio above which VACUUM runs (default: 0.20)
"""

from __future__ import annotations

import os
import sqlite3


def _size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def prune_snapshots(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete price snapshots older than the retention window.

    Returns the number of rows deleted.
    """
    before = conn.total_changes
    conn.execute(
        "DELETE FROM price_snapshots WHERE date(scanned_at) < date('now', 'localtime', ?)",
        (f"-{retention_days} days",),
    )
    conn.commit()
    return conn.total_changes - before


def prune_notification_log(conn: sqlite3.Connection) -> int:
    """Delete notification_log rows that are provably dead for dedup.

    The dedup consumers (was_notification_sent, get_last_notified_price)
    filter on departure_date = ? -- so two classes of rows can never again
    influence suppression:

    - Expired trips: departure_date more than one day in the past (UTC).
      The one-day grace encodes: uncertain calendar boundary -> preserve
      suppression state. False retention is acceptable, false deletion is
      not. Do not "optimize" the grace day away.
    - NULL departure_date: SQL equality never matches NULL, so these rows
      cannot satisfy any dedup query. All of them are dead regardless of
      age. (Distinct from NULL return_date, which is a live one-way trip
      matched via an explicit IS NULL branch.)

    Returns the number of rows deleted.
    """
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM notification_log
        WHERE departure_date IS NULL
           OR date(departure_date) < date('now', '-1 day')
        """
    )
    conn.commit()
    return conn.total_changes - before


def maybe_vacuum(conn: sqlite3.Connection, threshold: float) -> tuple[float, bool]:
    """VACUUM when the freelist ratio exceeds threshold.

    Must be called on a connection with no open transaction.
    Returns (freelist_ratio, vacuumed).
    """
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    ratio = freelist / page_count if page_count > 0 else 0.0
    if ratio > threshold:
        conn.execute("VACUUM")
        return ratio, True
    return ratio, False


def main() -> None:
    """Run the full maintenance pass using environment configuration."""
    db_path = os.environ["FLI_DB_PATH"]
    retention_days = int(os.environ.get("FLI_RETENTION_DAYS", "14"))
    vacuum_threshold = float(os.environ.get("FLI_VACUUM_THRESHOLD", "0.20"))

    print(f"DB size before maintenance: {_size_mb(db_path):.2f} MB")

    # Prune on one connection, then close before VACUUM.
    conn = sqlite3.connect(db_path)
    snap_deleted = prune_snapshots(conn, retention_days)
    notif_deleted = prune_notification_log(conn)
    conn.close()
    print(f"Pruned {snap_deleted} snapshots older than {retention_days} days")
    print(f"Pruned {notif_deleted} dead notification_log rows (expired or NULL departure)")

    # VACUUM on a fresh connection -- must not run inside a transaction.
    conn2 = sqlite3.connect(db_path)
    ratio, vacuumed = maybe_vacuum(conn2, vacuum_threshold)
    conn2.close()
    print(f"Freelist ratio: {ratio:.2%}")
    if vacuumed:
        print("VACUUM complete")
    else:
        print(f"VACUUM skipped (ratio {ratio:.2%} <= threshold {vacuum_threshold:.2%})")

    print(f"DB size after maintenance: {_size_mb(db_path):.2f} MB")


if __name__ == "__main__":
    main()

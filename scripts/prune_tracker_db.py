"""Prune old price snapshots and optionally VACUUM the tracker database.

Reads configuration from environment variables:
    FLI_DB_PATH          -- path to tracker.db (required)
    FLI_RETENTION_DAYS   -- days of snapshots to keep (default: 60)
    FLI_VACUUM_THRESHOLD -- freelist/page ratio above which VACUUM runs (default: 0.20)
"""

from __future__ import annotations

import os
import sqlite3

db_path = os.environ["FLI_DB_PATH"]
retention_days = int(os.environ.get("FLI_RETENTION_DAYS", "60"))
vacuum_threshold = float(os.environ.get("FLI_VACUUM_THRESHOLD", "0.20"))


def _size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


print(f"DB size before maintenance: {_size_mb(db_path):.2f} MB")

# Prune on one connection, then close before VACUUM.
conn = sqlite3.connect(db_path)
conn.execute(
    f"DELETE FROM price_snapshots"
    f" WHERE date(scanned_at) < date('now', 'localtime', '-{retention_days} days')"
)
deleted = conn.total_changes
conn.commit()
conn.close()
print(f"Pruned {deleted} snapshots older than {retention_days} days")

# Check freelist ratio on a fresh connection.
conn2 = sqlite3.connect(db_path)
page_count = conn2.execute("PRAGMA page_count").fetchone()[0]
freelist = conn2.execute("PRAGMA freelist_count").fetchone()[0]
conn2.close()
ratio = freelist / page_count if page_count > 0 else 0.0
print(f"Freelist ratio: {ratio:.2%} ({freelist}/{page_count} pages)")

# VACUUM on another fresh connection -- must not run inside a transaction.
if ratio > vacuum_threshold:
    conn3 = sqlite3.connect(db_path)
    conn3.execute("VACUUM")
    conn3.close()
    print("VACUUM complete")
else:
    print(f"VACUUM skipped (ratio {ratio:.2%} <= threshold {vacuum_threshold:.2%})")

print(f"DB size after maintenance: {_size_mb(db_path):.2f} MB")

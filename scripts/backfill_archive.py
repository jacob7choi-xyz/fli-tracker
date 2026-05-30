"""One-time backfill: export all snapshots from the pre-prune DB into the immutable archive.

Writes the same shard layout used by export_snapshots.py.

Environment variables:
    FULL_DB_PATH  -- path to the full (pre-prune) tracker.db
    ARCHIVE_DIR   -- root directory for archive files (same as live archive)

Groups rows by (date, route_group) and writes one shard per (date, group),
labeled run=backfill to distinguish from live per-sweep shards.
"""

from __future__ import annotations

import csv
import gzip
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from fli.tracker.regions import route_group

full_db_path = os.environ["FULL_DB_PATH"]
archive_dir = Path(os.environ["ARCHIVE_DIR"])

conn = sqlite3.connect(full_db_path)
conn.row_factory = sqlite3.Row

routes = {
    row["id"]: (row["origin"], row["destination"])
    for row in conn.execute("SELECT id, origin, destination FROM routes").fetchall()
}

print("Loading all snapshots from pre-prune DB...")
rows = conn.execute(
    """
    SELECT route_id, scanned_at, departure_date, return_date, price, currency
    FROM price_snapshots
    ORDER BY scanned_at, route_id, departure_date
    """
).fetchall()
conn.close()
print(f"Loaded {len(rows)} snapshots across {len(routes)} routes")

shards: dict[tuple[str, str], list[dict]] = defaultdict(list)
skipped = 0
for row in rows:
    route_id = row["route_id"]
    if route_id not in routes:
        skipped += 1
        continue
    origin, destination = routes[route_id]
    group = route_group(origin, destination)
    date = row["scanned_at"][:10]
    shards[(date, group)].append(dict(row))

if skipped:
    print(f"Skipped {skipped} rows with unknown route_id")

print(f"Writing {len(shards)} archive shards...")
for (date, group), shard_rows in sorted(shards.items()):
    out_path = archive_dir / f"date={date}" / f"group={group}" / "run=backfill.csv.gz"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "route_id",
                "scanned_at",
                "departure_date",
                "return_date",
                "price",
                "currency",
            ],  # noqa: E501
        )
        writer.writeheader()
        writer.writerows(shard_rows)
    print(f"  date={date} group={group}: {len(shard_rows)} rows")

print("Backfill complete")

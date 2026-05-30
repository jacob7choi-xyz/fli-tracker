"""Export price snapshots from one sweep window to an immutable archive shard.

Environment variables:
    FLI_DB_PATH  -- path to tracker.db
    ARCHIVE_DIR  -- root directory for archive files
    SWEEP_GROUP  -- route group: domestic, coastal, or longhaul
    SWEEP_START  -- inclusive lower bound, SQLite UTC format: YYYY-MM-DD HH:MM:SS
    SWEEP_END    -- exclusive upper bound, SQLite UTC format: YYYY-MM-DD HH:MM:SS

Output path:
    <ARCHIVE_DIR>/date=<YYYY-MM-DD>/group=<group>/run=<YYYY-MM-DDTHH-MM-SSZ>.csv.gz
"""

from __future__ import annotations

import csv
import gzip
import os
import sqlite3
from pathlib import Path

db_path = os.environ["FLI_DB_PATH"]
archive_dir = Path(os.environ["ARCHIVE_DIR"])
sweep_group = os.environ["SWEEP_GROUP"]
sweep_start = os.environ["SWEEP_START"]  # YYYY-MM-DD HH:MM:SS UTC
sweep_end = os.environ["SWEEP_END"]  # YYYY-MM-DD HH:MM:SS UTC

date_part = sweep_start[:10]
run_label = sweep_start.replace(" ", "T").replace(":", "-") + "Z"

out_path = archive_dir / f"date={date_part}" / f"group={sweep_group}" / f"run={run_label}.csv.gz"
out_path.parent.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT route_id, scanned_at, departure_date, return_date, price, currency
    FROM price_snapshots
    WHERE scanned_at >= ? AND scanned_at < ?
    ORDER BY scanned_at, route_id, departure_date
    """,
    (sweep_start, sweep_end),
).fetchall()
conn.close()

if not rows:
    print(f"No snapshots in [{sweep_start}, {sweep_end}) -- nothing to archive")
else:
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
        for row in rows:
            writer.writerow(dict(row))
    print(f"Archived {len(rows)} snapshots -> {out_path}")

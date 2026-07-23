"""Export one sweep window to an immutable archive shard, with provenance.

The archive shards are the authoritative ML dataset (Tier A); tracker.db is
non-authoritative operational state. Each workflow attempt also writes an
immutable provenance record runs/<attempt_id>.json carrying an explicit
collection_status: existence of a shard never implies a complete scheduled
observation. Partial shards are preserved, never silently promoted.

Environment variables:
    FLI_DB_PATH  -- path to tracker.db
    ARCHIVE_DIR  -- root directory for archive files
    SWEEP_GROUP  -- route group: domestic, coastal, or longhaul
    SWEEP_START  -- inclusive lower bound, SQLite UTC format: YYYY-MM-DD HH:MM:SS
    SWEEP_END    -- exclusive upper bound, SQLite UTC format: YYYY-MM-DD HH:MM:SS

Provenance (the manifest is skipped unless RUNS_DIR is set; when RUNS_DIR
is set, RUN_ID becomes REQUIRED -- attempt identity is never fabricated):
    RUNS_DIR          -- directory for runs/<attempt_id>.json records
    RUN_ID            -- GITHUB_RUN_ID (stable across re-runs); required
                         with RUNS_DIR
    RUN_ATTEMPT       -- GITHUB_RUN_ATTEMPT (increments per re-run)
    SWEEP_TRIGGER     -- github.event_name (schedule / workflow_dispatch)
    SLOT_OFFSET_HOURS -- this group's cron offset on the 6-hour grid
    FLI_SWEEP_RESULT  -- path to the sweep's explicit collection result JSON

Output path:
    <ARCHIVE_DIR>/date=<YYYY-MM-DD>/group=<group>/run=<YYYY-MM-DDTHH-MM-SSZ>.csv.gz
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

_FIELDS = ["route_id", "scanned_at", "departure_date", "return_date", "price", "currency"]


def export_shard(
    db_path: str, archive_dir: Path, group: str, sweep_start: str, sweep_end: str
) -> tuple[Path | None, int]:
    """Write the sweep window's snapshots to a csv.gz shard.

    The shard path embeds the sweep start timestamp, so a path can never be
    silently overwritten by a different execution. Returns (path, row_count);
    path is None when the window holds no rows (no shard is written).
    """
    date_part = sweep_start[:10]
    run_label = sweep_start.replace(" ", "T").replace(":", "-") + "Z"
    out_path = archive_dir / f"date={date_part}" / f"group={group}" / f"run={run_label}.csv.gz"

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
        return None, 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return out_path, len(rows)


def scheduled_slot(sweep_start: str, offset_hours: int) -> str:
    """Floor the sweep start onto this group's 6-hour cron grid (UTC).

    Cron delays and manual dispatches land between grid points; the slot is
    the grid point the run belongs to, letting coverage compare expected
    vs observed per slot. Datetime arithmetic handles day rollover when a
    delayed run lands before its group's first slot of the day.
    """
    start = datetime.strptime(sweep_start, "%Y-%m-%d %H:%M:%S")
    shifted = start - timedelta(hours=offset_hours)
    slot_shifted = shifted.replace(minute=0, second=0) - timedelta(hours=shifted.hour % 6)
    slot = slot_shifted + timedelta(hours=offset_hours)
    return slot.strftime("%Y-%m-%dT%H:%MZ")


def read_collection_result(result_path: str | None) -> dict | None:
    """Read the sweep's explicit collection result, if it exists and parses.

    A missing OR unreadable/malformed file means the sweep died before (or
    while) writing its result: the attempt is partial by definition, and a
    corrupt result file must never block archive publication (Tier A may
    not be gated on lower-tier inputs). Exit codes are never consulted --
    the scanner swallows per-search failures, so exit 0 proves nothing.
    """
    if not result_path or not os.path.exists(result_path):
        return None
    try:
        with open(result_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        print(f"Warning: sweep result unreadable ({result_path}); treating attempt as partial")
        return None


def build_manifest(
    *,
    attempt_id: str,
    group: str,
    trigger: str,
    sweep_start: str,
    sweep_end: str,
    slot_offset_hours: int,
    shard_path: str | None,
    row_count: int,
    sha256: str | None,
    result: dict | None,
) -> dict:
    """Assemble the provenance record for one workflow attempt.

    collection_status is complete only when the explicit result file exists
    and every intended unit completed. A unit is completed when its search
    returned (zero fares included); attempted or produced-results are not
    the test.
    """
    expected = result.get("expected_units") if result else None
    completed = result.get("completed_units") if result else None
    # complete requires BOTH counts present and equal. A schema-drifted
    # result file missing the keys would otherwise yield None == None and
    # fabricate completeness; absent evidence classifies as partial.
    complete = expected is not None and completed is not None and expected == completed
    return {
        "attempt_id": attempt_id,
        "scheduled_slot": scheduled_slot(sweep_start, slot_offset_hours),
        "group": group,
        "trigger": trigger,
        "sweep_start": sweep_start,
        "sweep_end": sweep_end,
        "shard_path": shard_path,
        "row_count": row_count,
        "sha256": sha256,
        "expected_units": expected,
        "completed_units": completed,
        "collection_status": "complete" if complete else "partial",
    }


def write_manifest(runs_dir: Path, manifest: dict) -> Path:
    """Write the immutable per-attempt provenance record.

    Integrity rule: same attempt_id + same content is an idempotent replay;
    same attempt_id + different content is an integrity failure and raises.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    out_path = runs_dir / f"{manifest['attempt_id']}.json"
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if out_path.exists():
        existing = out_path.read_text(encoding="utf-8")
        if existing != content:
            raise RuntimeError(
                f"Provenance integrity failure: {out_path} exists with different content"
            )
        return out_path
    out_path.write_text(content, encoding="utf-8")
    return out_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    """Export the shard, then write provenance when RUNS_DIR is configured."""
    db_path = os.environ["FLI_DB_PATH"]
    archive_dir = Path(os.environ["ARCHIVE_DIR"])
    group = os.environ["SWEEP_GROUP"]
    sweep_start = os.environ["SWEEP_START"]
    sweep_end = os.environ["SWEEP_END"]

    shard, row_count = export_shard(db_path, archive_dir, group, sweep_start, sweep_end)
    if shard is None:
        print(f"No snapshots in [{sweep_start}, {sweep_end}) -- nothing to archive")
    else:
        print(f"Archived {row_count} snapshots -> {shard}")

    runs_dir = os.environ.get("RUNS_DIR")
    if not runs_dir:
        return

    run_id = os.environ.get("RUN_ID")
    if not run_id:
        raise RuntimeError(
            "RUN_ID is required when RUNS_DIR is set (GITHUB_RUN_ID in CI); "
            "attempt identity is never fabricated"
        )

    result = read_collection_result(os.environ.get("FLI_SWEEP_RESULT"))
    manifest = build_manifest(
        attempt_id=f"{run_id}-{os.environ.get('RUN_ATTEMPT', '1')}",
        group=group,
        trigger=os.environ.get("SWEEP_TRIGGER", "unknown"),
        sweep_start=sweep_start,
        sweep_end=sweep_end,
        slot_offset_hours=int(os.environ.get("SLOT_OFFSET_HOURS", "0")),
        shard_path=str(shard.relative_to(archive_dir.parent)) if shard else None,
        row_count=row_count,
        sha256=_sha256(shard) if shard else None,
        result=result,
    )
    out = write_manifest(Path(runs_dir), manifest)
    print(f"Provenance: {out} (collection_status={manifest['collection_status']})")


if __name__ == "__main__":
    main()

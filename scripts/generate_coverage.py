"""Generate machine-readable collection coverage from the archive tree.

Produces coverage.csv with one row per (group, scheduled slot). This file,
not prose, is the authoritative provenance record for ML training code:
it says exactly which collection intervals exist and how trustworthy each
observation is.

Row status vocabulary:
    complete     -- a runs/ record exists and its collection_status is complete
    partial      -- a runs/ record exists and its collection_status is partial
    pre-manifest -- a shard exists but predates per-attempt provenance;
                    completeness is unknown and is NOT guessed
    missing      -- the slot falls inside the group's live range but no shard
                    or record exists, and the slot is not inside a declared
                    maintenance window. Covers both "the run failed" and "the
                    platform never created a run at all"; this file cannot
                    distinguish those and does not guess
    maintenance  -- collection was deliberately disabled for this slot, so the
                    absence is planned rather than a platform or code failure
    backfill     -- a run=backfill shard (day-granularity import, no slot)

This file is DERIVED, not authoritative. The evidence is the archive shards
and the immutable runs/ manifests; coverage.csv is a deterministic
materialization of them and can be rebuilt at any time.

Multiple attempts for one slot stay separately visible via attempt rows in
runs/; the slot row counts them in observed_attempts.

Environment variables:
    ARCHIVE_DIR -- archive root (date=.../group=.../run=...csv.gz)
    RUNS_DIR    -- per-attempt provenance directory (may not exist yet)
    COVERAGE_OUT -- output CSV path
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

# Cron offsets on the 6-hour grid, one per sweep group. These MUST match the
# cron expressions in .github/workflows/watch-*.yml: the expected-slot grid is
# derived from them, so a schedule change without a matching change here would
# silently mis-slot the entire historical record. test_generate_coverage.py
# parses the workflows and asserts they agree.
GROUP_OFFSETS = {"domestic": 0, "longhaul": 1, "coastal": 2}

# Slots where collection was deliberately off. Absence here is planned, not a
# failure, and is labeled maintenance so a future reader of this file alone can
# tell the two apart. Half-open [start, end).
MAINTENANCE_WINDOWS = [
    # 2026-07 incident: workflows disabled during remediation, restored 13:05Z.
    ("2026-07-23T18:00Z", "2026-07-24T13:05Z"),
]


def _in_maintenance(slot: str) -> bool:
    """Whether a scheduled slot falls inside a declared maintenance window."""
    return any(start <= slot < end for start, end in MAINTENANCE_WINDOWS)


_SHARD_RE = re.compile(
    r"date=(?P<date>[0-9-]+)/group=(?P<group>[a-z]+)/run=(?P<run>[^/]+)\.csv\.gz$"
)


def scheduled_slot(sweep_start: str, offset_hours: int) -> str:
    """Floor a sweep start (YYYY-MM-DD HH:MM:SS) onto the group's 6h grid."""
    start = datetime.strptime(sweep_start, "%Y-%m-%d %H:%M:%S")
    shifted = start - timedelta(hours=offset_hours)
    slot_shifted = shifted.replace(minute=0, second=0) - timedelta(hours=shifted.hour % 6)
    slot = slot_shifted + timedelta(hours=offset_hours)
    return slot.strftime("%Y-%m-%dT%H:%MZ")


def _run_label_to_sweep_start(run_label: str) -> str | None:
    """Convert run=2026-07-24T06-00-57Z back to SQLite format, if timestamped."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z$", run_label)
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}"


def scan_shards(archive_dir: Path) -> list[dict]:
    """Inventory every shard: group, slot (None for backfill), path."""
    shards = []
    for path in sorted(archive_dir.rglob("*.csv.gz")):
        m = _SHARD_RE.search(str(path))
        if not m:
            continue
        group = m.group("group")
        sweep_start = _run_label_to_sweep_start(m.group("run"))
        slot = scheduled_slot(sweep_start, GROUP_OFFSETS.get(group, 0)) if sweep_start else None
        shards.append({"group": group, "date": m.group("date"), "slot": slot, "path": str(path)})
    return shards


def scan_manifests(runs_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """Index runs/ records by (group, scheduled_slot)."""
    by_slot: dict[tuple[str, str], list[dict]] = {}
    if not runs_dir.is_dir():
        return by_slot
    for path in sorted(runs_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        key = (record["group"], record["scheduled_slot"])
        by_slot.setdefault(key, []).append(record)
    return by_slot


def _slot_grid(first_slot: str, last_slot: str, offset_hours: int) -> list[str]:
    """Every 6-hour grid point for one group, inclusive of both ends."""
    fmt = "%Y-%m-%dT%H:%MZ"
    current = datetime.strptime(first_slot, fmt)
    end = datetime.strptime(last_slot, fmt)
    out = []
    while current <= end:
        out.append(current.strftime(fmt))
        current += timedelta(hours=6)
    return out


def build_coverage(shards: list[dict], manifests: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """Assemble per-slot coverage rows across all groups.

    The live range per group runs from its first to its last timestamped
    shard or manifest slot; grid slots inside that range with no evidence
    are emitted as missing. Backfill shards get day-level rows. A slot is
    complete only if at least one of its attempts is complete; provenance
    is never guessed for pre-manifest shards.
    """
    rows: list[dict] = []
    groups = sorted({s["group"] for s in shards} | {g for g, _ in manifests})
    for group in groups:
        offset = GROUP_OFFSETS.get(group, 0)
        slot_shards: dict[str, list[dict]] = {}
        for s in shards:
            if s["group"] == group and s["slot"]:
                slot_shards.setdefault(s["slot"], []).append(s)
        slot_records = {slot: recs for (g, slot), recs in manifests.items() if g == group}

        for s in shards:
            if s["group"] == group and s["slot"] is None:
                rows.append(
                    {
                        "group": group,
                        "slot": f"{s['date']}(day)",
                        "observed_attempts": 1,
                        "status": "backfill",
                    }
                )

        known_slots = sorted(set(slot_shards) | set(slot_records))
        if not known_slots:
            continue
        for slot in _slot_grid(known_slots[0], known_slots[-1], offset):
            recs = slot_records.get(slot, [])
            n_shards = len(slot_shards.get(slot, []))
            observed = max(len(recs), n_shards)
            if recs:
                statuses = {r["collection_status"] for r in recs}
                status = "complete" if "complete" in statuses else "partial"
            elif n_shards:
                status = "pre-manifest"
            elif _in_maintenance(slot):
                status = "maintenance"
            else:
                status = "missing"
            rows.append(
                {
                    "group": group,
                    "slot": slot,
                    "observed_attempts": observed,
                    "status": status,
                }
            )
    return rows


def main() -> None:
    """Generate coverage.csv from environment configuration."""
    archive_dir = Path(os.environ["ARCHIVE_DIR"])
    runs_dir = Path(os.environ["RUNS_DIR"])
    out_path = Path(os.environ["COVERAGE_OUT"])

    rows = build_coverage(scan_shards(archive_dir), scan_manifests(runs_dir))
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["group", "slot", "observed_attempts", "status"])
        writer.writeheader()
        writer.writerows(rows)

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    print(f"Wrote {len(rows)} slot rows -> {out_path}")
    for status, count in sorted(by_status.items()):
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()

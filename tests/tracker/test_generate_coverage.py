"""Tests for scripts/generate_coverage.py.

Coverage classification contract: complete/partial come only from runs/
records; pre-manifest shards never have completeness guessed; grid slots
with no evidence inside the live range are missing; backfill shards are
day-granularity rows outside the slot grid.
"""

import gzip
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "generate_coverage",
    Path(__file__).resolve().parents[2] / "scripts" / "generate_coverage.py",
)
cov_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cov_mod)


def _write_shard(archive: Path, date: str, group: str, run_label: str) -> None:
    path = archive / f"date={date}" / f"group={group}" / f"run={run_label}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        f.write("route_id\n1\n")


def _write_manifest(runs: Path, attempt_id: str, group: str, slot: str, status: str) -> None:
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{attempt_id}.json").write_text(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "group": group,
                "scheduled_slot": slot,
                "collection_status": status,
            }
        )
    )


class TestBuildCoverage:
    def test_classification_across_statuses(self, tmp_path: Path):
        archive = tmp_path / "archive"
        runs = tmp_path / "runs"

        # Slot 00:00 - shard + complete manifest
        _write_shard(archive, "2026-07-24", "domestic", "run=stripped")  # ignored, bad name
        _write_shard(archive, "2026-07-24", "domestic", "2026-07-24T00-01-00Z")
        _write_manifest(runs, "1-1", "domestic", "2026-07-24T00:00Z", "complete")
        # Slot 06:00 - shard, no manifest (historical)
        _write_shard(archive, "2026-07-24", "domestic", "2026-07-24T06-02-00Z")
        # Slot 12:00 - nothing (missing, inside live range)
        # Slot 18:00 - manifest partial, shard present
        _write_shard(archive, "2026-07-24", "domestic", "2026-07-24T18-00-30Z")
        _write_manifest(runs, "4-1", "domestic", "2026-07-24T18:00Z", "partial")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        by_slot = {r["slot"]: r for r in rows if r["group"] == "domestic"}

        assert by_slot["2026-07-24T00:00Z"]["status"] == "complete"
        assert by_slot["2026-07-24T06:00Z"]["status"] == "pre-manifest"
        assert by_slot["2026-07-24T12:00Z"]["status"] == "missing"
        assert by_slot["2026-07-24T18:00Z"]["status"] == "partial"

    def test_backfill_is_day_granularity(self, tmp_path: Path):
        archive = tmp_path / "archive"
        _write_shard(archive, "2026-04-15", "domestic", "backfill")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), {})

        assert rows == [
            {
                "group": "domestic",
                "slot": "2026-04-15(day)",
                "observed_attempts": 1,
                "status": "backfill",
            }
        ]

    def test_multiple_attempts_per_slot_stay_visible(self, tmp_path: Path):
        archive = tmp_path / "archive"
        runs = tmp_path / "runs"
        _write_shard(archive, "2026-07-24", "coastal", "2026-07-24T02-01-00Z")
        _write_shard(archive, "2026-07-24", "coastal", "2026-07-24T02-30-00Z")
        _write_manifest(runs, "7-1", "coastal", "2026-07-24T02:00Z", "complete")
        _write_manifest(runs, "8-1", "coastal", "2026-07-24T02:00Z", "partial")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        (row,) = [r for r in rows if r["group"] == "coastal"]

        assert row["observed_attempts"] == 2
        # One complete attempt is enough for the slot to count as complete
        assert row["status"] == "complete"

    def test_group_offset_slots(self, tmp_path: Path):
        """Coastal runs at 02/08/14/20; a delayed 03:10 start belongs to 02:00."""
        archive = tmp_path / "archive"
        _write_shard(archive, "2026-07-24", "coastal", "2026-07-24T03-10-00Z")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), {})
        (row,) = rows

        assert row["slot"] == "2026-07-24T02:00Z"
        assert row["status"] == "pre-manifest"

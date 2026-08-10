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
        _write_shard(archive, "2026-08-20", "domestic", "run=stripped")  # ignored, bad name
        _write_shard(archive, "2026-08-20", "domestic", "2026-08-20T00-01-00Z")
        _write_manifest(runs, "1-1", "domestic", "2026-08-20T00:00Z", "complete")
        # Slot 06:00 - shard, no manifest (historical)
        _write_shard(archive, "2026-08-20", "domestic", "2026-08-20T06-02-00Z")
        # Slot 12:00 - nothing (missing, inside live range, no maintenance window)
        # Slot 18:00 - manifest partial, shard present
        _write_shard(archive, "2026-08-20", "domestic", "2026-08-20T18-00-30Z")
        _write_manifest(runs, "4-1", "domestic", "2026-08-20T18:00Z", "partial")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        by_slot = {r["slot"]: r for r in rows if r["group"] == "domestic"}

        assert by_slot["2026-08-20T00:00Z"]["status"] == "complete"
        assert by_slot["2026-08-20T06:00Z"]["status"] == "pre-manifest"
        assert by_slot["2026-08-20T12:00Z"]["status"] == "missing"
        assert by_slot["2026-08-20T18:00Z"]["status"] == "partial"

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


class TestScheduleModelMatchesWorkflows:
    """Guard the coupling between GROUP_OFFSETS and the workflow crons.

    The expected-slot grid is derived from GROUP_OFFSETS, so a schedule change
    without a matching generator change would silently mis-slot every
    historical row, which is worse than a stale file.
    """

    def test_offsets_match_workflow_crons(self):
        import re as _re

        root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        for group, offset in cov_mod.GROUP_OFFSETS.items():
            text = (root / f"watch-{group}.yml").read_text()
            cron = _re.search(r'cron:\s*"([^"]+)"', text).group(1)
            hour_field = cron.split()[1]
            if hour_field == "*/6":
                hours = [0, 6, 12, 18]
            else:
                hours = sorted(int(h) for h in hour_field.split(","))
            assert hours == [offset + 6 * i for i in range(4)], (
                f"{group}: cron hours {hours} disagree with GROUP_OFFSETS[{group}]={offset}"
            )

    def test_grid_step_is_six_hours(self):
        grid = cov_mod._slot_grid("2026-08-01T02:00Z", "2026-08-01T20:00Z", 2)
        assert grid == [
            "2026-08-01T02:00Z",
            "2026-08-01T08:00Z",
            "2026-08-01T14:00Z",
            "2026-08-01T20:00Z",
        ]


class TestAbsenceClassification:
    """Regression cases from the 2026-08-06 platform losses.

    Two slots produced no data by different mechanisms: one workflow run failed
    before acquiring a runner, and one scheduled run was never created at all.
    Both look identical from the repository's perspective, which is why the
    rollup classifies on absence of evidence rather than on cause.
    """

    def test_expected_slot_with_no_manifest_is_missing(self, tmp_path: Path):
        archive = tmp_path / "archive"
        runs = tmp_path / "runs"
        # Slots at 02:00 and 14:00 collected; 08:00 produced nothing at all.
        _write_shard(archive, "2026-08-06", "coastal", "2026-08-06T02-01-00Z")
        _write_manifest(runs, "1-1", "coastal", "2026-08-06T02:00Z", "complete")
        _write_shard(archive, "2026-08-06", "coastal", "2026-08-06T14-01-00Z")
        _write_manifest(runs, "2-1", "coastal", "2026-08-06T14:00Z", "complete")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        by_slot = {r["slot"]: r["status"] for r in rows}

        assert by_slot["2026-08-06T08:00Z"] == "missing"
        assert by_slot["2026-08-06T02:00Z"] == "complete"

    def test_maintenance_window_is_not_reported_as_missing(self, tmp_path: Path):
        """Deliberately disabled collection is planned absence, not failure."""
        archive = tmp_path / "archive"
        runs = tmp_path / "runs"
        _write_shard(archive, "2026-07-23", "domestic", "2026-07-23T12-00-00Z")
        _write_manifest(runs, "3-1", "domestic", "2026-07-23T12:00Z", "complete")
        _write_shard(archive, "2026-07-24", "domestic", "2026-07-24T18-00-00Z")
        _write_manifest(runs, "4-1", "domestic", "2026-07-24T18:00Z", "complete")

        rows = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        by_slot = {r["slot"]: r["status"] for r in rows}

        # Inside the declared window: planned, not a failure.
        assert by_slot["2026-07-23T18:00Z"] == "maintenance"
        assert by_slot["2026-07-24T00:00Z"] == "maintenance"
        assert by_slot["2026-07-24T06:00Z"] == "maintenance"
        # 12:00Z is still before the 13:05Z restoration, so also planned.
        assert by_slot["2026-07-24T12:00Z"] == "maintenance"

    def test_determinism(self, tmp_path: Path):
        """Same evidence must produce byte-identical output."""
        archive = tmp_path / "archive"
        runs = tmp_path / "runs"
        _write_shard(archive, "2026-08-06", "coastal", "2026-08-06T02-01-00Z")
        _write_manifest(runs, "1-1", "coastal", "2026-08-06T02:00Z", "complete")

        first = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        second = cov_mod.build_coverage(cov_mod.scan_shards(archive), cov_mod.scan_manifests(runs))
        assert first == second

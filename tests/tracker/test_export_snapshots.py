"""Tests for scripts/export_snapshots.py: shard export and provenance.

Invariants under test:
- Shard paths embed the sweep-start timestamp (structurally immutable).
- collection_status derives from the explicit collection result, never
  from exit codes: missing result file or mismatched unit counts = partial.
- Provenance records are immutable: same attempt + same content is an
  idempotent replay, same attempt + different content raises.
"""

import gzip
import importlib.util
import json
from pathlib import Path

import pytest

from fli.tracker.db import TrackerDB
from fli.tracker.models import PriceSnapshot, Route

_SPEC = importlib.util.spec_from_file_location(
    "export_snapshots",
    Path(__file__).resolve().parents[2] / "scripts" / "export_snapshots.py",
)
export_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export_mod)


@pytest.fixture()
def seeded_db(tmp_path: Path):
    """Provide a DB with one route and snapshots inside a known window."""
    db = TrackerDB(db_path=tmp_path / "test.db")
    route = db.add_route(Route(origin="DFW", destination="ORD"))
    db.add_snapshots(
        [
            PriceSnapshot(
                route_id=route.id,
                departure_date="2099-01-01",
                return_date="2099-01-08",
                price=150.0 + i,
                currency="USD",
            )
            for i in range(3)
        ]
    )
    # Pin scanned_at inside a deterministic window
    db._conn.execute("UPDATE price_snapshots SET scanned_at = '2026-07-24 06:05:00'")
    db._conn.commit()
    return db, tmp_path


class TestExportShard:
    def test_exports_rows_in_window(self, seeded_db):
        db, tmp_path = seeded_db
        shard, count = export_mod.export_shard(
            str(tmp_path / "test.db"),
            tmp_path / "archive",
            "domestic",
            "2026-07-24 06:00:00",
            "2026-07-24 07:00:00",
        )
        assert count == 3
        assert shard.name == "run=2026-07-24T06-00-00Z.csv.gz"
        assert shard.parent.name == "group=domestic"
        assert shard.parent.parent.name == "date=2026-07-24"
        with gzip.open(shard, "rt") as f:
            lines = f.read().strip().splitlines()
        assert len(lines) == 4  # header + 3 rows

    def test_empty_window_writes_nothing(self, seeded_db):
        db, tmp_path = seeded_db
        shard, count = export_mod.export_shard(
            str(tmp_path / "test.db"),
            tmp_path / "archive",
            "domestic",
            "2026-07-25 00:00:00",
            "2026-07-25 01:00:00",
        )
        assert shard is None
        assert count == 0
        assert not (tmp_path / "archive").exists()


class TestScheduledSlot:
    @pytest.mark.parametrize(
        ("sweep_start", "offset", "slot"),
        [
            ("2026-07-24 06:05:00", 0, "2026-07-24T06:00Z"),  # on-grid domestic
            ("2026-07-24 11:59:00", 0, "2026-07-24T06:00Z"),  # long cron delay
            ("2026-07-24 02:10:00", 2, "2026-07-24T02:00Z"),  # coastal offset
            ("2026-07-24 01:00:00", 1, "2026-07-24T01:00Z"),  # longhaul on-grid
            ("2026-07-24 00:30:00", 1, "2026-07-23T19:00Z"),  # rollover before first slot
            ("2026-07-24 23:59:00", 2, "2026-07-24T20:00Z"),  # last slot of day
        ],
    )
    def test_floors_to_group_grid(self, sweep_start, offset, slot):
        assert export_mod.scheduled_slot(sweep_start, offset) == slot


class TestCollectionStatus:
    def _manifest(self, result):
        return export_mod.build_manifest(
            attempt_id="123-1",
            group="domestic",
            trigger="schedule",
            sweep_start="2026-07-24 06:00:00",
            sweep_end="2026-07-24 06:10:00",
            slot_offset_hours=0,
            shard_path="archive/date=2026-07-24/group=domestic/run=x.csv.gz",
            row_count=10,
            sha256="abc",
            result=result,
        )

    def test_complete_when_counts_match(self):
        m = self._manifest({"expected_units": 90, "completed_units": 90})
        assert m["collection_status"] == "complete"

    def test_partial_when_units_missing(self):
        m = self._manifest({"expected_units": 90, "completed_units": 87})
        assert m["collection_status"] == "partial"

    def test_partial_when_no_result_file(self):
        """Treat a sweep that died before writing its result as partial.

        Shard existence never implies a complete observation.
        """
        m = self._manifest(None)
        assert m["collection_status"] == "partial"
        assert m["expected_units"] is None

    def test_missing_result_path_reads_none(self, tmp_path):
        assert export_mod.read_collection_result(None) is None
        assert export_mod.read_collection_result(str(tmp_path / "absent.json")) is None


class TestManifestImmutability:
    def _manifest(self):
        return {
            "attempt_id": "555-1",
            "collection_status": "complete",
            "row_count": 5,
        }

    def test_writes_record(self, tmp_path):
        out = export_mod.write_manifest(tmp_path / "runs", self._manifest())
        assert out.name == "555-1.json"
        assert json.loads(out.read_text())["row_count"] == 5

    def test_identical_replay_is_idempotent(self, tmp_path):
        export_mod.write_manifest(tmp_path / "runs", self._manifest())
        out = export_mod.write_manifest(tmp_path / "runs", self._manifest())
        assert json.loads(out.read_text())["row_count"] == 5

    def test_conflicting_content_raises(self, tmp_path):
        export_mod.write_manifest(tmp_path / "runs", self._manifest())
        changed = self._manifest() | {"row_count": 6}
        with pytest.raises(RuntimeError, match="integrity"):
            export_mod.write_manifest(tmp_path / "runs", changed)

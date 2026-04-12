"""Tests for the track CLI command."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.tracker.db import TrackerDB

runner = CliRunner()


@pytest.fixture()
def db_path(tmp_path: Path):
    """Return a temp DB path and patch _get_db to use it."""
    path = tmp_path / "test.db"
    # Pre-create the DB so schema exists
    db = TrackerDB(db_path=path)
    db.close()

    def _make_db():
        return TrackerDB(db_path=path)

    with patch("fli.cli.commands.track._get_db", side_effect=_make_db):
        yield path


class TestTrackAddDefaults:
    """Tests for default values when adding routes via CLI."""

    def test_look_ahead_defaults_to_45(self, db_path):
        """Route created without --look-ahead gets look_ahead=45, matching model and schema."""
        result = runner.invoke(app, ["track", "add", "DFW", "ORD"])
        assert result.exit_code == 0

        db = TrackerDB(db_path=db_path)
        routes = db.list_routes(active_only=False)
        db.close()
        assert len(routes) == 1
        assert routes[0].look_ahead == 45

    def test_look_ahead_can_be_overridden(self, db_path):
        result = runner.invoke(app, ["track", "add", "DFW", "ORD", "--look-ahead", "60"])
        assert result.exit_code == 0

        db = TrackerDB(db_path=db_path)
        routes = db.list_routes(active_only=False)
        db.close()
        assert routes[0].look_ahead == 60

    def test_durations_default_to_7(self, db_path):
        result = runner.invoke(app, ["track", "add", "DFW", "ORD"])
        assert result.exit_code == 0

        db = TrackerDB(db_path=db_path)
        routes = db.list_routes(active_only=False)
        db.close()
        assert routes[0].durations == [7]

    def test_round_trip_by_default(self, db_path):
        result = runner.invoke(app, ["track", "add", "DFW", "ORD"])
        assert result.exit_code == 0

        db = TrackerDB(db_path=db_path)
        routes = db.list_routes(active_only=False)
        db.close()
        assert routes[0].is_round_trip is True


class TestTrackAddValidation:
    """Tests for input validation in track add."""

    def test_same_origin_destination_rejected(self, db_path):
        result = runner.invoke(app, ["track", "add", "DFW", "DFW"])
        assert result.exit_code != 0
        assert "different" in result.output.lower()

    def test_invalid_airport_rejected(self, db_path):
        result = runner.invoke(app, ["track", "add", "DFW", "ZZZZZ"])
        assert result.exit_code != 0

    @pytest.mark.parametrize("bad_dur", ["0", "-1", "22", "abc"])
    def test_invalid_durations_rejected(self, db_path, bad_dur):
        result = runner.invoke(app, ["track", "add", "DFW", "ORD", "-d", bad_dur])
        assert result.exit_code != 0

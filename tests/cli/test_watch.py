"""Tests for the watch CLI command."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.tracker.db import TrackerDB
from fli.tracker.models import Route

runner = CliRunner()


@pytest.fixture()
def mock_db(tmp_path: Path):
    """Create a temporary TrackerDB and patch it into the watch command."""
    db = TrackerDB(db_path=tmp_path / "test.db")
    with patch("fli.cli.commands.watch.TrackerDB", return_value=db):
        yield db
    db.close()


class TestWatchGroupFlag:
    """Tests for the --group / -g CLI option."""

    def test_invalid_group_rejected(self, mock_db):
        result = runner.invoke(app, ["watch", "--group", "bogus"])
        assert result.exit_code != 0
        assert "Invalid group" in result.output

    def test_uppercase_group_accepted(self, mock_db):
        """Case normalization: 'DOMESTIC' should be accepted as 'domestic'."""
        mock_db.add_route(Route(origin="DFW", destination="ORD"))

        with patch("fli.cli.commands.watch.scan_route", return_value=[]):
            result = runner.invoke(app, ["watch", "--group", "DOMESTIC"])

        assert result.exit_code == 0

    def test_mixed_case_group_accepted(self, mock_db):
        """Case normalization: 'LongHaul' should be accepted as 'longhaul'."""
        mock_db.add_route(Route(origin="DFW", destination="FCO"))

        with patch("fli.cli.commands.watch.scan_route", return_value=[]):
            result = runner.invoke(app, ["watch", "--group", "LongHaul"])

        assert result.exit_code == 0

    @pytest.mark.parametrize("group", ["domestic", "longhaul"])
    def test_valid_groups_accepted(self, mock_db, group):
        result = runner.invoke(app, ["watch", "--group", group])
        # Should not fail on validation (may exit 0 with "No active routes")
        assert "Invalid group" not in result.output

    def test_no_routes_in_group_shows_message(self, mock_db):
        mock_db.add_route(Route(origin="DFW", destination="FCO"))  # longhaul

        result = runner.invoke(app, ["watch", "--group", "domestic"])
        assert "No active routes" in result.output

"""Tests for tracker CLI commands.

Uses a temporary database via monkeypatching to avoid touching
the real ~/.fli/tracker.db.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.tracker.db import TrackerDB
from fli.tracker.models import Alert, AlertType, PriceSnapshot, Route


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def tmp_db(tmp_path: Path):
    """Create a temporary TrackerDB and patch all CLI modules to use it."""
    db = TrackerDB(db_path=tmp_path / "test.db")
    # Prevent CLI commands from closing the shared test DB
    db.close = lambda: None

    with (
        patch("fli.cli.commands.track._get_db", return_value=db),
        patch("fli.cli.commands.alert._get_db", return_value=db),
        patch("fli.cli.commands.watch.TrackerDB", return_value=db),
        patch("fli.cli.commands.history.TrackerDB", return_value=db),
    ):
        yield db


# ------------------------------------------------------------------
# track commands
# ------------------------------------------------------------------


class TestTrackAdd:
    def test_add_route(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "add", "DFW", "FCO"])
        assert result.exit_code == 0
        assert "Added route" in result.output
        assert "DFW" in result.output
        assert "FCO" in result.output

    def test_add_route_with_options(self, runner, tmp_db):
        result = runner.invoke(
            app,
            [
                "track",
                "add",
                "JFK",
                "LHR",
                "--cabin",
                "business",
                "--stops",
                "non_stop",
                "--durations",
                "7,10,14",
                "--max-price",
                "800",
            ],
        )
        assert result.exit_code == 0
        routes = tmp_db.list_routes()
        assert len(routes) == 1
        assert routes[0].cabin_class == "BUSINESS"
        assert routes[0].durations == [7, 10, 14]
        assert routes[0].max_price == 800.0

    def test_add_one_way(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "add", "DFW", "FCO", "--one-way"])
        assert result.exit_code == 0
        assert "one-way" in result.output

    def test_add_invalid_airport(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "add", "ZZZ", "FCO"])
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_add_same_origin_destination(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "add", "DFW", "DFW"])
        assert result.exit_code == 1
        assert "different" in result.output


class TestTrackList:
    def test_list_empty(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "list"])
        assert result.exit_code == 0
        assert "No tracked routes" in result.output

    def test_list_with_routes(self, runner, tmp_db):
        tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["track", "list"])
        assert result.exit_code == 0
        assert "DFW" in result.output
        assert "FCO" in result.output


class TestTrackRemove:
    def test_remove_route(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["track", "remove", str(route.id)])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_nonexistent(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "remove", "999"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestTrackPauseResume:
    def test_pause_and_resume(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))

        result = runner.invoke(app, ["track", "pause", str(route.id)])
        assert result.exit_code == 0
        assert "Paused" in result.output

        result = runner.invoke(app, ["track", "resume", str(route.id)])
        assert result.exit_code == 0
        assert "Resumed" in result.output


class TestTrackSnooze:
    def test_snooze_default_7_days(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["track", "snooze", str(route.id)])
        assert result.exit_code == 0
        assert "Snoozed" in result.output
        assert "until" in result.output

    def test_snooze_custom_days(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["track", "snooze", str(route.id), "--days", "14"])
        assert result.exit_code == 0
        assert "Snoozed" in result.output

    def test_snooze_zero_days_rejected(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["track", "snooze", str(route.id), "--days", "0"])
        assert result.exit_code == 1

    def test_snooze_nonexistent_route(self, runner, tmp_db):
        result = runner.invoke(app, ["track", "snooze", "999"])
        assert result.exit_code == 1

    def test_unsnooze_wakes_early(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        runner.invoke(app, ["track", "snooze", str(route.id), "--days", "7"])
        result = runner.invoke(app, ["track", "unsnooze", str(route.id)])
        assert result.exit_code == 0
        assert "Unsnoozed" in result.output

    def test_unsnooze_not_snoozed_shows_friendly_message(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["track", "unsnooze", str(route.id)])
        assert result.exit_code == 0
        assert "not currently snoozed" in result.output

    def test_list_shows_snoozed_status(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        runner.invoke(app, ["track", "snooze", str(route.id), "--days", "7"])
        result = runner.invoke(app, ["track", "list", "--all"])
        assert result.exit_code == 0
        assert "snoozed" in result.output.lower()


# ------------------------------------------------------------------
# alert commands
# ------------------------------------------------------------------


class TestAlertAdd:
    def test_add_threshold_alert(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(
            app, ["alert", "add", str(route.id), "--below", "500", "--notify", "test://url"]
        )
        assert result.exit_code == 0
        assert "threshold" in result.output

    def test_add_drop_alert(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(
            app, ["alert", "add", str(route.id), "--drop", "--notify", "test://url"]
        )
        assert result.exit_code == 0
        assert "drop" in result.output

    def test_add_requires_type(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["alert", "add", str(route.id), "--notify", "test://url"])
        assert result.exit_code == 1
        assert "--below" in result.output or "--drop" in result.output

    def test_add_to_nonexistent_route(self, runner, tmp_db):
        result = runner.invoke(app, ["alert", "add", "999", "--drop", "--notify", "test://url"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAlertList:
    def test_list_empty(self, runner, tmp_db):
        result = runner.invoke(app, ["alert", "list"])
        assert result.exit_code == 0
        assert "No alerts" in result.output

    def test_list_with_alerts(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        tmp_db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.THRESHOLD,
                threshold=500.0,
                notify_url="test://url",
            )
        )
        result = runner.invoke(app, ["alert", "list"])
        assert result.exit_code == 0
        assert "threshold" in result.output


class TestAlertRemove:
    def test_remove_alert(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        alert = tmp_db.add_alert(
            Alert(
                route_id=route.id,
                alert_type=AlertType.DROP,
                notify_url="test://url",
            )
        )
        result = runner.invoke(app, ["alert", "remove", str(alert.id)])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_nonexistent(self, runner, tmp_db):
        result = runner.invoke(app, ["alert", "remove", "999"])
        assert result.exit_code == 1


# ------------------------------------------------------------------
# watch command
# ------------------------------------------------------------------


class TestWatch:
    @patch("fli.cli.commands.watch.scan_route")
    @patch("fli.cli.commands.watch.check_alerts", return_value=[])
    def test_watch_single_route(self, mock_check, mock_scan, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        mock_scan.return_value = [
            PriceSnapshot(
                route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
            )
        ]

        result = runner.invoke(app, ["watch", "--route", str(route.id)])
        assert result.exit_code == 0
        assert "Stored" in result.output

    @patch("fli.cli.commands.watch.scan_route")
    @patch("fli.cli.commands.watch.check_alerts", return_value=[])
    def test_watch_all_routes(self, mock_check, mock_scan, runner, tmp_db):
        tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        tmp_db.add_route(Route(origin="JFK", destination="LHR"))
        mock_scan.return_value = [
            PriceSnapshot(route_id=1, departure_date="2026-07-15", price=500.0, currency="USD")
        ]

        result = runner.invoke(app, ["watch"])
        assert result.exit_code == 0
        assert "Sweep complete" in result.output

    def test_watch_no_routes(self, runner, tmp_db):
        result = runner.invoke(app, ["watch"])
        assert result.exit_code == 0
        assert "No active routes" in result.output

    def test_watch_nonexistent_route(self, runner, tmp_db):
        result = runner.invoke(app, ["watch", "--route", "999"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_watch_paused_route(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        tmp_db.set_route_active(route.id, False)

        result = runner.invoke(app, ["watch", "--route", str(route.id)])
        assert result.exit_code == 1
        assert "paused" in result.output


# ------------------------------------------------------------------
# history command
# ------------------------------------------------------------------


class TestHistory:
    def test_history_empty(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        result = runner.invoke(app, ["history", str(route.id)])
        assert result.exit_code == 0
        assert "No price history" in result.output

    def test_history_with_data(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        tmp_db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-16", price=450.0, currency="USD"
                ),
            ]
        )

        result = runner.invoke(app, ["history", str(route.id)])
        assert result.exit_code == 0
        assert "DFW" in result.output
        assert "FCO" in result.output

    def test_history_nonexistent_route(self, runner, tmp_db):
        result = runner.invoke(app, ["history", "999"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_history_with_date_filter(self, runner, tmp_db):
        route = tmp_db.add_route(Route(origin="DFW", destination="FCO"))
        tmp_db.add_snapshots(
            [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
                ),
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-16", price=450.0, currency="USD"
                ),
            ]
        )

        result = runner.invoke(app, ["history", str(route.id), "--date", "2026-07-15"])
        assert result.exit_code == 0

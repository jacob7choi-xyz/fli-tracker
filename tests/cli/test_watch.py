"""Tests for the watch CLI command."""

import io
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import apprise
import pytest
from typer.testing import CliRunner

from fli.cli.commands import watch as watch_mod
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


class TestVerboseLogRedaction:
    """`fli watch --verbose` must not print notification-target identifiers.

    The sweep workflows run `fli watch --verbose` in a public repository, so
    every log line is world-readable. Apprise's email plugin logs
    "Sent Email to <recipient>" at INFO, and the recipient is derived from
    NOTIFY_URL. GitHub masks only the exact secret string, not values derived
    from it. The existing notifier secret test mocks Apprise entirely, so it
    could not see this; these tests run the real Apprise email plugin with
    only the SMTP connection faked.
    """

    RECIPIENT = "sentinel-user@example.com"
    PASSWORD = "SENTINEL-c9f2-PW"
    URL = f"mailto://sentinel-user:{PASSWORD}@example.com"

    @pytest.fixture()
    def verbose_log(self):
        """Install the --verbose handler on a buffer; restore logging after."""
        root = logging.getLogger()
        saved = (list(root.handlers), root.level, logging.getLogger("apprise").level)
        stream = io.StringIO()
        handler = watch_mod._configure_logging(stream)
        yield stream
        root.removeHandler(handler)
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        logging.getLogger("apprise").setLevel(saved[2])

    def _send_real_apprise_email(self) -> bool:
        with patch("apprise.plugins.email.base.smtplib") as smtp:
            smtp.SMTP.return_value = MagicMock()
            smtp.SMTP_SSL.return_value = MagicMock()
            ap = apprise.Apprise()
            ap.add(self.URL)
            sent = ap.notify(body="body", title="title")
            # Non-vacuity: the plugin must have reached its SMTP send path,
            # which is where the recipient line is logged.
            assert smtp.SMTP.called or smtp.SMTP_SSL.called
        return sent

    def test_delivery_logs_no_recipient_or_password(self, verbose_log):
        assert self._send_real_apprise_email() is True
        logging.getLogger("fli.tracker.notifier").info("Digest sent: %d alerts", 1)

        out = verbose_log.getvalue()
        # Non-vacuity: the handler is live and fli's own INFO lines survive.
        assert "Digest sent: 1 alerts" in out
        assert self.RECIPIENT not in out
        assert self.PASSWORD not in out
        # Apprise INFO is suppressed outright, not just redacted: this is what
        # covers non-email targets the address pattern cannot recognize.
        assert "Sent Email" not in out

    @pytest.mark.parametrize(
        "emit",
        [
            pytest.param(lambda log, addr: log.warning("bounce from %s", addr), id="warning"),
            pytest.param(lambda log, addr: log.info("to=" + addr), id="preformatted"),
            pytest.param(
                lambda log, addr: log.error("failed", exc_info=RuntimeError(f"rcpt {addr}")),
                id="traceback",
            ),
        ],
    )
    def test_any_logger_any_level_is_redacted(self, verbose_log, emit):
        emit(logging.getLogger("some.third.party"), self.RECIPIENT)
        out = verbose_log.getvalue()
        assert out, "nothing was logged, so absence would prove nothing"
        assert self.RECIPIENT not in out

    @pytest.mark.parametrize(("flags", "expected_calls"), [(["--verbose"], 1), ([], 0)])
    def test_verbose_flag_installs_the_handler(self, mock_db, flags, expected_calls):
        with patch.object(watch_mod, "_configure_logging") as configure:
            runner.invoke(app, ["watch", "--group", "domestic", *flags])
        assert configure.call_count == expected_calls

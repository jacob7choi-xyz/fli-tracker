"""CLI command for running price sweeps."""

import json
import logging
import os
import re
from typing import Annotated

import typer

from fli.tracker.db import TrackerDB
from fli.tracker.detector import check_alerts
from fli.tracker.notifier import NotificationError, send_digest
from fli.tracker.regions import RouteGroup, route_group
from fli.tracker.scanner import ScanStats, route_units, scan_route

logger = logging.getLogger(__name__)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class _RedactingFormatter(logging.Formatter):
    """Mask email addresses in the fully formatted line, tracebacks included.

    The sweeps run `fli watch --verbose` in a public repository, so every log
    line is world-readable. Apprise logs "Sent Email to <recipient>", and the
    recipient is derived from NOTIFY_URL; GitHub masks only the exact secret
    string. Redacting the final string covers every logger, every level, and
    exception text, without trusting third-party log call sites.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the record, then replace anything shaped like an address."""
        return _EMAIL_RE.sub("<redacted-email>", super().format(record))


def _configure_logging(stream=None) -> logging.Handler:
    """Attach the --verbose log handler to the root logger at INFO.

    Apprise is held at WARNING: its INFO lines describe delivery targets,
    which may be identifiers the email pattern does not recognize if
    NOTIFY_URL ever points at a non-email service. fli's own
    "Digest sent: N alerts" remains the delivery signal.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_RedactingFormatter("%(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("apprise").setLevel(logging.WARNING)
    return handler


def _send_digest_contained(triggers, db) -> tuple[int, str | None]:
    """Send the digest without letting notification failure abort the sweep.

    Collection and persistence happen before this point; NO notification
    failure (config, dependency, or delivery) may cost collected data --
    and every one of them must surface identically as a red run. Returns
    (notifications_sent, failure_reason_or_None).
    """
    try:
        return send_digest(triggers, db), None
    except NotificationError as exc:
        logger.error("Notification failed: %s", exc)
        return 0, str(exc)


def _write_sweep_result(
    stats: ScanStats,
    snapshots: int,
    alerts_triggered: int,
    notifications_sent: int,
    notify_failed: bool,
) -> None:
    """Write the explicit collection result to FLI_SWEEP_RESULT, if set.

    This file, not the process exit code, is the source of truth for
    collection completeness: the scanner swallows per-search failures by
    design, so exit 0 does not imply every intended unit was collected.
    Consumers treat a missing file as an incomplete (crashed) sweep.
    """
    result_path = os.environ.get("FLI_SWEEP_RESULT")
    if not result_path:
        return
    payload = {
        "expected_units": stats.expected_units,
        "completed_units": stats.completed_units,
        "snapshots": snapshots,
        "alerts_triggered": alerts_triggered,
        "notifications_sent": notifications_sent,
        "notify_failed": notify_failed,
    }
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def watch(
    route_id: Annotated[
        int | None,
        typer.Option("--route", "-r", help="Scan a specific route ID only"),
    ] = None,
    group: Annotated[
        str | None,
        typer.Option("--group", "-g", help="Route group: 'domestic', 'coastal', or 'longhaul'"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed output"),
    ] = False,
):
    """Run a single price sweep of active routes.

    Scans tracked routes, stores price snapshots, checks alerts,
    and sends a single digest notification for all triggers.

    Use --group to scan a subset of routes:
        fli watch --group domestic
        fli watch --group coastal
        fli watch --group longhaul
    """
    if group is not None:
        group = group.lower()
        if group not in ("domestic", "coastal", "longhaul"):
            typer.echo(f"Invalid group '{group}'. Use 'domestic', 'coastal', or 'longhaul'.")
            raise typer.Exit(1)

    group_filter: RouteGroup | None = group  # type: ignore[assignment]

    if verbose:
        _configure_logging()

    db = TrackerDB()
    try:
        if route_id is not None:
            route = db.get_route(route_id)
            if route is None:
                typer.echo(f"Route {route_id} not found")
                raise typer.Exit(1)
            if not route.active:
                typer.echo(f"Route {route_id} is paused. Use 'fli track resume {route_id}' first.")
                raise typer.Exit(1)

            typer.echo(f"Scanning route {route.id}: {route.origin} -> {route.destination}")
            stats = ScanStats(expected_units=route_units(route))
            snapshots = scan_route(route, stats=stats)

            sent = 0
            triggers = []
            notify_failure = None
            if snapshots:
                # Check alerts BEFORE inserting so get_min_price
                # reflects the previous low, not the current scan
                triggers = check_alerts(db, route, snapshots)

                db.add_snapshots(snapshots)
                typer.echo(f"Stored {len(snapshots)} price snapshots")

                if triggers:
                    sent, notify_failure = _send_digest_contained(triggers, db)
                    typer.echo(f"Sent digest with {sent}/{len(triggers)} alerts")
                else:
                    typer.echo("No alerts triggered")
            else:
                typer.echo("No results found")

            _write_sweep_result(
                stats, len(snapshots), len(triggers), sent, notify_failure is not None
            )
            if notify_failure is not None:
                typer.echo(f"Notification failed: {notify_failure}", err=True)
                raise typer.Exit(2)
        else:
            routes = db.list_routes(active_only=True)
            if group_filter is not None:
                routes = [r for r in routes if route_group(r.origin, r.destination) == group_filter]
            if not routes:
                label = f" in group '{group_filter}'" if group_filter else ""
                typer.echo(
                    f"No active routes to scan{label}. Add one with: fli track add <origin> <dest>"
                )
                raise typer.Exit()

            group_label = f" ({group_filter})" if group_filter else ""
            typer.echo(f"Scanning {len(routes)} active route(s){group_label}...")
            total_snapshots = 0
            all_triggers = []
            stats = ScanStats()

            for route in routes:
                typer.echo(f"  {route.origin} -> {route.destination}...", nl=False)
                # Count intent before scanning so units the scanner never
                # reaches still register as expected-but-not-completed
                stats.expected_units += route_units(route)
                snapshots = scan_route(route, stats=stats)

                if snapshots:
                    # Check alerts BEFORE inserting so get_min_price
                    # reflects the previous low, not the current scan
                    triggers = check_alerts(db, route, snapshots)

                    db.add_snapshots(snapshots)
                    total_snapshots += len(snapshots)
                    typer.echo(f" {len(snapshots)} prices", nl=False)

                    if triggers:
                        all_triggers.extend(triggers)
                        typer.echo(f", {len(triggers)} alert(s)")
                    else:
                        typer.echo("")
                else:
                    typer.echo(" no results")

            # Send one digest for all triggers from the sweep
            total_sent = 0
            notify_failure = None
            if all_triggers:
                total_sent, notify_failure = _send_digest_contained(all_triggers, db)

            typer.echo(
                f"Sweep complete: {total_snapshots} snapshots, "
                f"{len(all_triggers)} alerts triggered, "
                f"{total_sent} notifications sent "
                f"({stats.completed_units}/{stats.expected_units} units collected)"
            )
            _write_sweep_result(
                stats, total_snapshots, len(all_triggers), total_sent, notify_failure is not None
            )
            if notify_failure is not None:
                typer.echo(f"Notification failed: {notify_failure}", err=True)
                raise typer.Exit(2)
    finally:
        db.close()

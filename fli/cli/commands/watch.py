"""CLI command for running price sweeps."""

import logging
from typing import Annotated

import typer

from fli.tracker.db import TrackerDB
from fli.tracker.detector import check_alerts
from fli.tracker.notifier import send_digest
from fli.tracker.regions import RouteGroup, route_group
from fli.tracker.scanner import scan_route


def watch(
    route_id: Annotated[
        int | None,
        typer.Option("--route", "-r", help="Scan a specific route ID only"),
    ] = None,
    group: Annotated[
        str | None,
        typer.Option("--group", "-g", help="Route group: 'domestic' or 'longhaul'"),
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
        fli watch --group longhaul
    """
    if group is not None:
        group = group.lower()
        if group not in ("domestic", "longhaul"):
            typer.echo(f"Invalid group '{group}'. Use 'domestic' or 'longhaul'.")
            raise typer.Exit(1)

    group_filter: RouteGroup | None = group  # type: ignore[assignment]

    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db = TrackerDB()
    try:
        if route_id is not None:
            route = db.get_route(route_id)
            if route is None:
                typer.echo(f"Route {route_id} not found")
                raise typer.Exit(1)
            if not route.active:
                typer.echo(
                    f"Route {route_id} is paused."
                    f" Use 'fli track resume {route_id}' first."
                )
                raise typer.Exit(1)

            typer.echo(f"Scanning route {route.id}: {route.origin} -> {route.destination}")
            snapshots = scan_route(route)

            if snapshots:
                # Check alerts BEFORE inserting so get_min_price
                # reflects the previous low, not the current scan
                triggers = check_alerts(db, route, snapshots)

                db.add_snapshots(snapshots)
                typer.echo(f"Stored {len(snapshots)} price snapshots")

                if triggers:
                    sent = send_digest(triggers, db)
                    typer.echo(f"Sent digest with {sent}/{len(triggers)} alerts")
                else:
                    typer.echo("No alerts triggered")
            else:
                typer.echo("No results found")
        else:
            routes = db.list_routes(active_only=True)
            if group_filter is not None:
                routes = [
                    r for r in routes
                    if route_group(r.origin, r.destination) == group_filter
                ]
            if not routes:
                label = f" in group '{group_filter}'" if group_filter else ""
                typer.echo(
                    f"No active routes to scan{label}."
                    " Add one with: fli track add <origin> <dest>"
                )
                raise typer.Exit()

            group_label = f" ({group_filter})" if group_filter else ""
            typer.echo(f"Scanning {len(routes)} active route(s){group_label}...")
            total_snapshots = 0
            all_triggers = []

            for route in routes:
                typer.echo(f"  {route.origin} -> {route.destination}...", nl=False)
                snapshots = scan_route(route)

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
            if all_triggers:
                total_sent = send_digest(all_triggers, db)

            typer.echo(
                f"Sweep complete: {total_snapshots} snapshots, "
                f"{len(all_triggers)} alerts triggered, "
                f"{total_sent} notifications sent"
            )
    finally:
        db.close()

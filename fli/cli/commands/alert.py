"""CLI commands for managing price alerts."""

from typing import Annotated

import typer

from fli.cli.console import console
from fli.tracker.db import TrackerDB
from fli.tracker.models import Alert, AlertType

alert_app = typer.Typer(help="Manage price alerts")


def _get_db() -> TrackerDB:
    return TrackerDB()


@alert_app.command(name="add")
def alert_add(
    route_id: Annotated[int, typer.Argument(help="Route ID to attach alert to")],
    below: Annotated[
        float | None,
        typer.Option("--below", "-b", help="Price threshold (alert when price is at or below)"),
    ] = None,
    drop: Annotated[
        bool,
        typer.Option("--drop", help="Alert on new all-time low prices"),
    ] = False,
    notify_url: Annotated[
        str | None,
        typer.Option(
            "--notify",
            "-n",
            help="Deprecated and ignored. Set the NOTIFY_URL environment variable instead.",
        ),
    ] = None,
):
    """Add a price alert to a tracked route.

    Use --below for threshold alerts, --drop for all-time low alerts.
    At least one of --below or --drop must be specified.

    Notification delivery is configured via the NOTIFY_URL environment
    variable (an Apprise URL), never stored in the database.

    Example:
        fli alert add 1 --below 500
        fli alert add 1 --drop

    """
    if below is None and not drop:
        typer.echo("Error: Specify --below <price> or --drop (or both)")
        raise typer.Exit(1)

    if notify_url is not None:
        typer.echo(
            "Warning: --notify is deprecated and ignored. Notification URLs are "
            "not stored; set the NOTIFY_URL environment variable instead.",
            err=True,
        )

    db = _get_db()
    route = db.get_route(route_id)
    if route is None:
        typer.echo(f"Route {route_id} not found")
        db.close()
        raise typer.Exit(1)

    alerts_added = []

    if below is not None:
        alert = db.add_alert(
            Alert(
                route_id=route_id,
                alert_type=AlertType.THRESHOLD,
                threshold=below,
            )
        )
        alerts_added.append(f"threshold (below {below}), id={alert.id}")

    if drop:
        alert = db.add_alert(
            Alert(
                route_id=route_id,
                alert_type=AlertType.DROP,
            )
        )
        alerts_added.append(f"drop (all-time low), id={alert.id}")

    db.close()

    for desc in alerts_added:
        typer.echo(f"Added alert: {desc} for {route.origin} -> {route.destination}")


@alert_app.command(name="list")
def alert_list(
    route_id: Annotated[
        int | None,
        typer.Option("--route", "-r", help="Filter by route ID"),
    ] = None,
    all_alerts: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include inactive alerts"),
    ] = False,
):
    """List configured alerts."""
    db = _get_db()
    alerts = db.list_alerts(route_id=route_id, active_only=not all_alerts)

    if not alerts:
        typer.echo("No alerts configured.")
        db.close()
        raise typer.Exit()

    from rich.table import Table

    table = Table(title="Price Alerts")
    table.add_column("ID", style="cyan")
    table.add_column("Route")
    table.add_column("Type")
    table.add_column("Threshold")
    table.add_column("Notify")
    table.add_column("Status")

    for a in alerts:
        route = db.get_route(a.route_id)
        route_str = f"{route.origin} -> {route.destination}" if route else f"route {a.route_id}"
        threshold_str = f"{a.threshold:.2f}" if a.threshold is not None else "-"
        status = "active" if a.active else "inactive"
        table.add_row(
            str(a.id),
            route_str,
            a.alert_type.value,
            threshold_str,
            "configured via NOTIFY_URL",
            status,
        )

    db.close()
    console.print(table)


@alert_app.command(name="remove")
def alert_remove(
    alert_id: Annotated[int, typer.Argument(help="Alert ID to remove")],
):
    """Remove an alert."""
    db = _get_db()
    try:
        if db.remove_alert(alert_id):
            typer.echo(f"Removed alert {alert_id}")
        else:
            typer.echo(f"Alert {alert_id} not found")
            raise typer.Exit(1)
    finally:
        db.close()

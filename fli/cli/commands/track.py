"""CLI commands for managing tracked routes."""

from datetime import date, timedelta
from typing import Annotated

import typer

from fli.cli.console import console
from fli.core.parsers import ParseError, resolve_airport
from fli.tracker.db import TrackerDB
from fli.tracker.models import Route

track_app = typer.Typer(help="Manage tracked flight routes")


def _get_db() -> TrackerDB:
    return TrackerDB()


def _parse_durations(raw: str) -> list[int]:
    """Parse comma-separated durations into sorted unique positive ints."""
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        val = int(part)
        if val < 1 or val > 21:
            raise ValueError(f"Duration must be 1-21 days, got {val}")
        values.append(val)
    if not values:
        raise ValueError("At least one duration is required")
    return sorted(set(values))


@track_app.command(name="add")
def track_add(
    origin: Annotated[str, typer.Argument(help="Departure airport IATA code")],
    destination: Annotated[str, typer.Argument(help="Arrival airport IATA code")],
    cabin_class: Annotated[
        str,
        typer.Option(
            "--cabin", "-c", help="Cabin class (ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST)"
        ),
    ] = "ECONOMY",
    max_stops: Annotated[
        str,
        typer.Option("--stops", "-s", help="Max stops (ANY, NON_STOP, ONE_STOP, TWO_PLUS_STOPS)"),
    ] = "ANY",
    durations: Annotated[
        str,
        typer.Option(
            "--durations",
            "-d",
            help="Trip durations in days, comma-separated (e.g. 5,7,10)",
        ),
    ] = "7",
    look_ahead: Annotated[
        int,
        typer.Option("--look-ahead", "-l", help="Days ahead to scan"),
    ] = 45,
    one_way: Annotated[
        bool,
        typer.Option("--one-way", help="Track as one-way (default is round-trip)"),
    ] = False,
    max_price: Annotated[
        float | None,
        typer.Option("--max-price", help="Only alert for fares at or below this price"),
    ] = None,
):
    """Add a route to track."""
    try:
        resolve_airport(origin)
        resolve_airport(destination)
    except ParseError as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(1) from e

    if origin.upper() == destination.upper():
        typer.echo("Error: Origin and destination must be different")
        raise typer.Exit(1)

    try:
        parsed_durations = _parse_durations(durations)
    except ValueError as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(1) from e

    db = _get_db()
    route = db.add_route(
        Route(
            origin=origin.upper(),
            destination=destination.upper(),
            cabin_class=cabin_class.upper(),
            max_stops=max_stops.upper(),
            durations=parsed_durations,
            look_ahead=look_ahead,
            is_round_trip=not one_way,
            max_price=max_price,
        )
    )
    db.close()

    dur_str = ",".join(str(d) for d in parsed_durations)
    trip_type = "one-way" if one_way else f"round-trip ({dur_str}d)"
    price_str = f" [max ${max_price:.0f}]" if max_price else ""
    typer.echo(
        f"Added route {route.id}: {origin.upper()} -> {destination.upper()}"
        f" [{trip_type}]{price_str}"
    )


@track_app.command(name="list")
def track_list(
    all_routes: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include paused routes"),
    ] = False,
):
    """List tracked routes."""
    db = _get_db()
    routes = db.list_routes(active_only=not all_routes)
    db.close()

    if not routes:
        typer.echo("No tracked routes.")
        raise typer.Exit()

    from rich.table import Table

    table = Table(title="Tracked Routes")
    table.add_column("ID", style="cyan")
    table.add_column("Route")
    table.add_column("Durations")
    table.add_column("Max Price")
    table.add_column("Cabin")
    table.add_column("Stops")
    table.add_column("Look-ahead")
    table.add_column("Status")

    today = date.today().isoformat()
    for r in routes:
        if r.is_round_trip:
            dur_str = "RT (" + ",".join(str(d) for d in r.durations) + "d)"
        else:
            dur_str = "OW"
        price_str = f"${r.max_price:.0f}" if r.max_price else "-"
        if not r.active:
            status = "paused"
        elif r.snoozed_until and r.snoozed_until >= today:
            status = f"snoozed until {r.snoozed_until}"
        else:
            status = "active"
        table.add_row(
            str(r.id),
            f"{r.origin} -> {r.destination}",
            dur_str,
            price_str,
            r.cabin_class,
            r.max_stops,
            f"{r.look_ahead}d",
            status,
        )

    console.print(table)


@track_app.command(name="remove")
def track_remove(
    route_id: Annotated[int, typer.Argument(help="Route ID to remove")],
):
    """Remove a tracked route and all its data."""
    db = _get_db()
    try:
        if db.remove_route(route_id):
            typer.echo(f"Removed route {route_id}")
        else:
            typer.echo(f"Route {route_id} not found")
            raise typer.Exit(1)
    finally:
        db.close()


@track_app.command(name="pause")
def track_pause(
    route_id: Annotated[int, typer.Argument(help="Route ID to pause")],
):
    """Pause a tracked route (skip during sweeps)."""
    db = _get_db()
    try:
        if db.set_route_active(route_id, False):
            typer.echo(f"Paused route {route_id}")
        else:
            typer.echo(f"Route {route_id} not found")
            raise typer.Exit(1)
    finally:
        db.close()


@track_app.command(name="resume")
def track_resume(
    route_id: Annotated[int, typer.Argument(help="Route ID to resume")],
):
    """Resume a paused route."""
    db = _get_db()
    try:
        if db.set_route_active(route_id, True):
            typer.echo(f"Resumed route {route_id}")
        else:
            typer.echo(f"Route {route_id} not found")
            raise typer.Exit(1)
    finally:
        db.close()


@track_app.command(name="snooze")
def track_snooze(
    route_id: Annotated[int, typer.Argument(help="Route ID to snooze")],
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to snooze (default 7)"),
    ] = 7,
):
    """Temporarily mute a route for N days. Alerts auto-resume after expiry."""
    if days < 1:
        typer.echo("Error: --days must be at least 1")
        raise typer.Exit(1)

    until = (date.today() + timedelta(days=days)).isoformat()
    db = _get_db()
    try:
        route = db.get_route(route_id)
        if route is None:
            typer.echo(f"Route {route_id} not found")
            raise typer.Exit(1)
        db.snooze_route(route_id, until)
        typer.echo(
            f"Snoozed route {route_id} ({route.origin} -> {route.destination}) until {until}"
        )
    finally:
        db.close()


@track_app.command(name="unsnooze")
def track_unsnooze(
    route_id: Annotated[int, typer.Argument(help="Route ID to wake early")],
):
    """Wake a snoozed route before its expiry."""
    db = _get_db()
    try:
        route = db.get_route(route_id)
        if route is None:
            typer.echo(f"Route {route_id} not found")
            raise typer.Exit(1)
        today = date.today().isoformat()
        if not route.snoozed_until or route.snoozed_until < today:
            typer.echo(
                f"Route {route_id} ({route.origin} -> {route.destination})"
                " is not currently snoozed."
            )
            return
        db.unsnooze_route(route_id)
        typer.echo(f"Unsnoozed route {route_id} ({route.origin} -> {route.destination})")
    finally:
        db.close()

"""CLI commands for managing tracked routes."""

from typing import Annotated

import typer

from fli.cli.console import console
from fli.core.parsers import ParseError, resolve_airport
from fli.tracker.db import TrackerDB
from fli.tracker.models import Route

track_app = typer.Typer(help="Manage tracked flight routes")


def _get_db() -> TrackerDB:
    return TrackerDB()


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
    trip_duration: Annotated[
        int,
        typer.Option("--duration", "-d", help="Round-trip duration in days"),
    ] = 7,
    look_ahead: Annotated[
        int,
        typer.Option("--look-ahead", "-l", help="Days ahead to scan"),
    ] = 90,
    one_way: Annotated[
        bool,
        typer.Option("--one-way", help="Track as one-way (default is round-trip)"),
    ] = False,
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

    db = _get_db()
    route = db.add_route(
        Route(
            origin=origin.upper(),
            destination=destination.upper(),
            cabin_class=cabin_class.upper(),
            max_stops=max_stops.upper(),
            trip_duration=trip_duration,
            look_ahead=look_ahead,
            is_round_trip=not one_way,
        )
    )
    db.close()

    trip_type = "one-way" if one_way else f"round-trip ({trip_duration}d)"
    typer.echo(f"Added route {route.id}: {origin.upper()} -> {destination.upper()} [{trip_type}]")


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
    table.add_column("Type")
    table.add_column("Cabin")
    table.add_column("Stops")
    table.add_column("Look-ahead")
    table.add_column("Status")

    for r in routes:
        trip_type = f"RT ({r.trip_duration}d)" if r.is_round_trip else "OW"
        status = "active" if r.active else "paused"
        table.add_row(
            str(r.id),
            f"{r.origin} -> {r.destination}",
            trip_type,
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

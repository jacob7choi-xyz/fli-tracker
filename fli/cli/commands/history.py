"""CLI command for viewing price history."""

from typing import Annotated

import typer

from fli.cli.console import console
from fli.tracker.db import TrackerDB


def history(
    route_id: Annotated[int, typer.Argument(help="Route ID to show history for")],
    departure_date: Annotated[
        str | None,
        typer.Option("--date", "-d", help="Filter by departure date (YYYY-MM-DD)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of records to show"),
    ] = 50,
    chart: Annotated[
        bool,
        typer.Option("--chart", help="Show ASCII price chart"),
    ] = False,
):
    """Show price history for a tracked route."""
    db = TrackerDB()
    try:
        route = db.get_route(route_id)

        if route is None:
            typer.echo(f"Route {route_id} not found")
            raise typer.Exit(1)

        snapshots = db.get_snapshots(
            route_id, departure_date=departure_date, limit=limit
        )
    finally:
        db.close()

    if not snapshots:
        typer.echo(f"No price history for route {route_id} ({route.origin} -> {route.destination})")
        raise typer.Exit()

    typer.echo(f"Price history: {route.origin} -> {route.destination}")

    if chart:
        _show_chart(snapshots, route)
    else:
        _show_table(snapshots)


def _show_table(snapshots):
    """Display price history as a table."""
    from rich.table import Table

    table = Table()
    table.add_column("Departure", style="cyan")
    table.add_column("Return")
    table.add_column("Price", justify="right")
    table.add_column("Currency")
    table.add_column("Scanned At")

    for snap in snapshots:
        table.add_row(
            snap.departure_date,
            snap.return_date or "-",
            f"{snap.price:.2f}",
            snap.currency,
            snap.scanned_at.strftime("%Y-%m-%d %H:%M") if snap.scanned_at else "-",
        )

    console.print(table)


def _show_chart(snapshots, route):
    """Display an ASCII price chart."""
    try:
        import plotext as plt
    except ImportError as e:
        typer.echo("plotext is required for charts. Install with: uv add plotext")
        raise typer.Exit(1) from e

    # Group by departure date and show min price per date
    date_prices: dict[str, float] = {}
    for snap in reversed(snapshots):
        if snap.departure_date not in date_prices:
            date_prices[snap.departure_date] = snap.price
        else:
            date_prices[snap.departure_date] = min(date_prices[snap.departure_date], snap.price)

    if not date_prices:
        typer.echo("No data to chart")
        raise typer.Exit()

    dates = list(date_prices.keys())
    prices = list(date_prices.values())

    plt.clear_figure()
    plt.plot(prices, marker="braille")
    plt.title(f"Lowest prices: {route.origin} -> {route.destination}")
    plt.ylabel(f"Price ({snapshots[0].currency})")
    plt.xlabel("Departure date index")
    plt.show()

    # Show date range
    typer.echo(f"Dates: {dates[0]} to {dates[-1]} ({len(dates)} dates)")
    typer.echo(f"Range: {min(prices):.2f} - {max(prices):.2f} {snapshots[0].currency}")

#!/usr/bin/env python3
"""CLI entry point for the fli flight search tool."""

import sys

import typer

from fli.cli.commands.alert import alert_app
from fli.cli.commands.dates import dates
from fli.cli.commands.flights import flights
from fli.cli.commands.history import history
from fli.cli.commands.track import track_app
from fli.cli.commands.watch import watch

app = typer.Typer(
    help="Search for flights using Google Flights data",
    add_completion=True,
)

# Register commands
app.command(name="flights")(flights)
app.command(name="dates")(dates)
app.command(name="watch")(watch)
app.command(name="history")(history)
app.add_typer(track_app, name="track")
app.add_typer(alert_app, name="alert")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """Search for flights using Google Flights data.

    If no command is provided, show help.
    """
    if ctx.invoked_subcommand is None:
        ctx.get_help()
        raise typer.Exit()


def cli():
    """Entry point for the CLI that handles default command."""
    args = sys.argv[1:]
    if not args:
        sys.argv.append("--help")
        args.append("--help")

    # If the first argument isn't a command, treat as flights search
    known_commands = ["flights", "dates", "track", "watch", "history", "alert", "--help", "-h"]
    if args[0] not in known_commands:
        sys.argv.insert(1, "flights")

    app()


if __name__ == "__main__":
    cli()

# fli-tracker

An autonomous flight-price collection system built on [`fli`](https://github.com/punitarani/fli),
a library that reads Google Flights through its API rather than by scraping.

The tracker sweeps a set of routes every six hours, archives every price
observation with provenance, scores fares against their own history, and emails
a digest when something is genuinely cheap. It has been running since April
2026.

**[Postmortem: seven weeks of silent collection loss](docs/incidents/2026-07-silent-collection-loss.md)** (draft)

In June 2026 this system kept sending deal alerts while quietly failing to save
any data, losing roughly half of June and three quarters of July before anyone
noticed. Most of the architecture here exists because of what that incident
taught: failure domain separation, archive first publication, fail closed
writes, per run provenance, and fault injection to prove the failure paths
behave as designed.

> **Relationship to upstream:** the search library, CLI, and MCP server are
> [punitarani/fli](https://github.com/punitarani/fli). The price tracker
> (`fli/tracker/`), its collection workflows, provenance tooling, and incident
> documentation are additions in this repository.

## Underlying library: fast, scraping-free flight search

* **Fast**: direct API access instead of HTML parsing
* **Zero scraping**: no browser automation
* **Reliable**: less prone to breaking on UI changes
* **Modular**: extensible architecture

## MCP Server

```bash
pipx install flights

# Run the MCP server on STDIO
fli-mcp

# Run the MCP server over HTTP (streamable)
fli-mcp-http  # serves at http://127.0.0.1:8000/mcp/
```

![MCP Demo](https://github.com/punitarani/fli/blob/main/data/mcp-demo.gif)

### Connecting to Claude Desktop

```json
{
  "mcpServers": {
    "fli": {
      "command": "/Users/<user>/.local/bin/fli-mcp"
    }
  }
}
```

> **Note**: Replace `<user>` with your actual username.
> You can also find the path to the MCP server by running `which fli-mcp` in your terminal.

### MCP Tools Available

The MCP server provides two main tools:

| Tool                 | Description                                                 |
|----------------------|-------------------------------------------------------------|
| **`search_flights`** | Search for flights on a specific date with detailed filters |
| **`search_dates`**   | Find the cheapest travel dates across a flexible date range |

#### `search_flights` Parameters

| Parameter          | Type   | Description                                         |
|--------------------|--------|-----------------------------------------------------|
| `origin`           | string | Departure airport IATA code (e.g., 'JFK')           |
| `destination`      | string | Arrival airport IATA code (e.g., 'LHR')             |
| `departure_date`   | string | Travel date in YYYY-MM-DD format                    |
| `return_date`      | string | Return date for round trips (optional)              |
| `cabin_class`      | string | ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST        |
| `max_stops`        | string | ANY, NON_STOP, ONE_STOP, or TWO_PLUS_STOPS          |
| `departure_window` | string | Time window in 'HH-HH' format (e.g., '6-20')        |
| `airlines`         | list   | Filter by airline codes (e.g., ['BA', 'AA'])        |
| `sort_by`          | string | CHEAPEST, DURATION, DEPARTURE_TIME, or ARRIVAL_TIME |
| `passengers`       | int    | Number of adult passengers                          |

#### `search_dates` Parameters

| Parameter          | Type   | Description                                  |
|--------------------|--------|----------------------------------------------|
| `origin`           | string | Departure airport IATA code (e.g., 'JFK')    |
| `destination`      | string | Arrival airport IATA code (e.g., 'LHR')      |
| `start_date`       | string | Start of date range in YYYY-MM-DD format     |
| `end_date`         | string | End of date range in YYYY-MM-DD format       |
| `trip_duration`    | int    | Trip duration in days (for round-trips)      |
| `is_round_trip`    | bool   | Whether to search for round-trip flights     |
| `cabin_class`      | string | ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST |
| `max_stops`        | string | ANY, NON_STOP, ONE_STOP, or TWO_PLUS_STOPS   |
| `departure_window` | string | Time window in 'HH-HH' format (e.g., '6-20') |
| `airlines`         | list   | Filter by airline codes (e.g., ['BA', 'AA']) |
| `sort_by_price`    | bool   | Sort results by price (lowest first)         |
| `passengers`       | int    | Number of adult passengers                   |

## Quick Start

```bash
pip install flights
```

```bash
# Install using pipx (recommended for CLI)
pipx install flights

# Get started with CLI
fli --help
```

![CLI Demo](https://github.com/punitarani/fli/blob/main/data/cli-demo.png)

## Features

* 🔍 **Powerful Search**
    * One-way flight searches
    * Flexible departure times
    * Multi-airline support
    * Cabin class selection
    * Stop preferences
    * Custom result sorting

* 💺 **Cabin Classes**
    * Economy
    * Premium Economy
    * Business
    * First

* 🎯 **Smart Sorting**
    * Price
    * Duration
    * Departure Time
    * Arrival Time

* 🛡️ **Built-in Protection**
    * Rate limiting
    * Automatic retries
    * Comprehensive error handling
    * Input validation

## Price Tracker

The tracker turns the search library into an autonomous data-collection system:
it sweeps a set of routes every six hours, archives every observation with
provenance, scores fares against their own price history, and emails a digest
when something is genuinely cheap.

It has been collecting since April 2026. In June it broke silently and lost six
weeks of data before anyone noticed.
**[Read the postmortem.](docs/incidents/2026-07-silent-collection-loss.md)**
Most of the architecture below exists because of what that incident taught.

### Architecture

Subsystems are classified by loss tolerance. Failure propagates upward in
visibility but never in destructive authority: a lower tier failing may turn a
run red, but may never block or destroy a higher tier.

| Tier | Component | Loss tolerance |
|---|---|---|
| **A** | Archive shards (`archive/date=.../group=.../run=....csv.gz`) | Authoritative observations. Immutable, append-only, published first |
| **B** | `tracker.db` | Operational state: routes, alerts, suppression history. Rebuildable inconvenience |
| **C** | Notifications | Side effects. Never permitted to gate A or B |

Properties that follow from that ordering:

- **Archive first publication.** The shard and its provenance record are pushed
  in their own transaction before the database is touched, so a database failure
  cannot cost observations.
- **Fail closed writes.** Push failures are classified by SHA identity rather
  than exit code. Remote equal to local means the push landed and the response
  was lost. Remote equal to the starting tip means a transient failure. Anything
  else means the single writer contract was violated, and the run stops for a
  human to investigate.
- **Per attempt provenance.** Every run writes an immutable record with its
  scheduled slot, sweep window, shard checksum, and an explicit
  `collection_status`, derived by comparing expected against completed
  collection units rather than reading the process exit code. The existence of a
  shard never implies a complete observation window.
- **Size gates.** A warning at 50 MiB; at 90 MiB the database snapshot is
  refused while the archive stays durable and the run goes red.
- **Runtime only credentials.** The notification URL is read from the
  environment at send time and fails closed when absent. Nothing is persisted in
  application state, displayed by the CLI, or written to logs, and tests assert
  its absence from database bytes, stdout, stderr, logs, and exception text.
- **Delivery truthful suppression.** Deduplication rows are written only after
  confirmed delivery, so a failed send never records "already notified." The
  governing bias is false duplicate over false suppression.

### Quick Start

```bash
# Install with tracker dependencies
uv sync --extra tracker

# Notification target is read from the environment, never stored
export NOTIFY_URL="mailto://user:app_password@gmail.com?to=you@gmail.com"

# Add a route with flexible durations and a price cap
fli track add DFW FCO --cabin ECONOMY --durations "7,10,14" --max-price 550 --look-ahead 90

# Alert when the price hits a new all-time low
fli alert add 1 --drop

# Run a manual sweep
fli watch --verbose

# View price history
fli history 1 --chart
```

### Tracker Commands

| Command | Description |
|---------|-------------|
| `fli track add` | Add a route to monitor (supports `--durations`, `--max-price`) |
| `fli track list` | List tracked routes with durations and price caps |
| `fli track pause/resume` | Pause or resume a route |
| `fli track snooze/unsnooze` | Silence a route for N days, with auto-wake |
| `fli track remove` | Remove a route and its data |
| `fli alert add` | Add a price alert (threshold or all-time-low detection) |
| `fli alert list` | List configured alerts |
| `fli alert remove` | Remove an alert |
| `fli watch` | Run a single price sweep (`--group` to scan a subset) |
| `fli history` | View price history (table or ASCII chart) |

### Deal Scoring

A composite 0-100 score computed against each route's own price history, so
"cheap" means cheap *for that route* rather than cheap in absolute terms:

| Component | Points | What it measures |
|---|---|---|
| Z-score | 0-40 | Standard deviations below the route mean, normalized by that route's own volatility |
| Percentile rank | 0-30 | Position in the route's empirical price distribution |
| Lead time | 0-20 | Compared with the typical price for this booking window (0-7, 8-14, 15-30, 31-60 days out) |
| Seasonality | 0-10 | Cheap fare during a peak travel month |

| Score | Label |
|-------|-------|
| 80-100 | BUY NOW |
| 60-79 | Strong buy |
| 40-59 | Worth watching |
| 20-39 | Meh |
| 0-19 | Skip |

A confidence gate prevents misleading ratings: routes with fewer than 14 days of
history or 10 snapshots show "Building history..." instead of a score.

> **Honest caveat:** the component weights are hand picked rather than learned.
> Validating them requires labeled outcomes, meaning a record of which fares were
> actually booked, and that dataset does not exist yet. The features are sound
> inputs for an eventual model. The weights on top are judgment.

### Automated Collection

Three GitHub Actions workflows sweep on a staggered six-hour schedule, sharing a
concurrency group so only one writer touches the data branch at a time:

| Workflow | Routes | Schedule (UTC) |
|---|---|---|
| `watch-domestic.yml` | US, Caribbean, Mexico, Central America, Canada | 00/06/12/18 |
| `watch-longhaul.yml` | Europe, Asia, South America, everything else | 01/07/13/19 |
| `watch-coastal.yml` | Major US coastal metros | 02/08/14/20 |

Observations are persisted to a dedicated `data` branch:

```
archive/date=2026-07-25/group=domestic/run=2026-07-25T12-55-31Z.csv.gz   # Tier A
runs/30094859362-1.json                                                  # provenance
coverage.csv                                                             # per-slot completeness
tracker.db                                                               # Tier B
```

`coverage.csv` records every scheduled slot as `complete`, `partial`,
`pre-manifest`, `missing`, or `backfill`, so downstream analysis can weight
windows by trustworthiness instead of assuming uniform quality, including across
the June and July gap.

---

## CLI Usage

### Search for Flights

```bash
# Basic flight search
fli flights JFK LHR 2026-10-25

# Advanced search with filters
fli flights JFK LHR 2026-10-25 \
    --time 6-20 \             # Departure time window (6 AM - 8 PM)
    --airlines BA KL \        # Airlines (British Airways, KLM)
    --class BUSINESS \        # Cabin class
    --stops NON_STOP \        # Non-stop flights only
    --sort DURATION           # Sort by duration
```

> ⚠️ **Experimental**
> `--format json` is experimental. The JSON schema may change while the machine-readable CLI contract settles.
>
> ```bash
> # Return machine-readable flight results
> fli flights JFK LHR 2026-10-25 --format json
> ```

### Find Cheapest Dates

```bash
# Basic date search
fli dates JFK LHR

# Advanced search with date range
fli dates JFK LHR \
    --from 2026-01-01 \
    --to 2026-02-01 \
    --monday --friday      # Only Mondays and Fridays
```

> ⚠️ **Experimental**
> `--format json` is experimental for date searches as well.
>
> ```bash
> # Return machine-readable date search results
> fli dates JFK LHR --from 2026-01-01 --to 2026-02-01 --format json
> ```

### CLI Options

#### Flights Command (`fli flights`)

| Option           | Description           | Example                |
|------------------|-----------------------|------------------------|
| `--return, -r`   | Return date           | `2026-10-30`           |
| `--time, -t`     | Departure time window | `6-20`                 |
| `--airlines, -a` | Airline IATA codes    | `BA KL`                |
| `--class, -c`    | Cabin class           | `ECONOMY`, `BUSINESS`  |
| `--stops, -s`    | Maximum stops         | `NON_STOP`, `ONE_STOP` |
| `--sort, -o`     | Sort results by       | `CHEAPEST`, `DURATION` |
| `--format`       | Output format         | `text`, `json`         |

#### Dates Command (`fli dates`)

| Option             | Description            | Example                |
|--------------------|------------------------|------------------------|
| `--from`           | Start date             | `2026-01-01`           |
| `--to`             | End date               | `2026-02-01`           |
| `--duration, -d`   | Trip duration in days  | `3`                    |
| `--round, -R`      | Round-trip search      | (flag)                 |
| `--airlines, -a`   | Airline IATA codes     | `BA KL`                |
| `--class, -c`      | Cabin class            | `ECONOMY`, `BUSINESS`  |
| `--stops, -s`      | Maximum stops          | `NON_STOP`, `ONE_STOP` |
| `--time`           | Departure time window  | `6-20`                 |
| `--sort`           | Sort by price          | (flag)                 |
| `--[day]`          | Day filters            | `--monday`, `--friday` |
| `--format`         | Output format          | `text`, `json`         |

## MCP Server Integration

Fli includes a Model Context Protocol (MCP) server that allows AI assistants like Claude to search for flights directly.
This enables natural language flight search through conversation.

### Running the MCP Server

```bash
# Run the MCP server on STDIO
fli-mcp

# Or with uv (for development)
uv run fli-mcp

# Or with make (for development)
make mcp

# Run the MCP server over HTTP (streamable)
fli-mcp-http  # serves at http://127.0.0.1:8000/mcp/
```

### Claude Desktop Configuration

To use the flight search capabilities in Claude Desktop, add this configuration to your `claude_desktop_config.json`:

**Location**: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

```json
{
  "mcpServers": {
    "flight-search": {
      "command": "fli-mcp",
      "args": []
    }
  }
}
```

After adding this configuration:

1. Restart Claude Desktop
2. You can now ask Claude to search for flights naturally:
    * "Find flights from JFK to LAX on December 25th"
    * "What are the cheapest dates to fly from NYC to London in January?"
    * "Search for business class flights from SFO to NRT with no stops"

## Python API Usage

### Basic Search Example

```python
from datetime import datetime, timedelta
from fli.models import (
    Airport,
    PassengerInfo,
    SeatType,
    MaxStops,
    SortBy,
    FlightSearchFilters,
    FlightSegment
)
from fli.search import SearchFlights

# Create search filters
filters = FlightSearchFilters(
    passenger_info=PassengerInfo(adults=1),
    flight_segments=[
        FlightSegment(
            departure_airport=[[Airport.JFK, 0]],
            arrival_airport=[[Airport.LAX, 0]],
            travel_date=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        )
    ],
    seat_type=SeatType.ECONOMY,
    stops=MaxStops.NON_STOP,
    sort_by=SortBy.CHEAPEST,
)

# Search flights
search = SearchFlights()
flights = search.search(filters)

# Process results
for flight in flights:
    print(f"💰 Price: ${flight.price}")
    print(f"⏱️ Duration: {flight.duration} minutes")
    print(f"✈️ Stops: {flight.stops}")

    for leg in flight.legs:
        print(f"\n🛫 Flight: {leg.airline.value} {leg.flight_number}")
        print(f"📍 From: {leg.departure_airport.value} at {leg.departure_datetime}")
        print(f"📍 To: {leg.arrival_airport.value} at {leg.arrival_datetime}")
```

### Running Examples

We provide 11 comprehensive examples in the `examples/` directory that demonstrate various use cases:

```bash
# Run examples with uv (recommended)
uv run python examples/basic_one_way_search.py
uv run python examples/round_trip_search.py
uv run python examples/date_range_search.py

# Or install dependencies first, then run directly
pip install pydantic curl_cffi httpx
python examples/basic_one_way_search.py
```

**Available Examples:**

* `basic_one_way_search.py` - Simple one-way flight search
* `round_trip_search.py` - Round-trip flight booking
* `date_range_search.py` - Find cheapest dates
* `complex_flight_search.py` - Advanced filtering and multi-passenger
* `time_restrictions_search.py` - Time-based filtering
* `date_search_with_preferences.py` - Weekend filtering
* `price_tracking.py` - Price monitoring over time
* `error_handling_with_retries.py` - Robust error handling
* `result_processing.py` - Data analysis with pandas
* `complex_round_trip_validation.py` - Advanced round-trip with validation
* `advanced_date_search_validation.py` - Complex date search with filtering

> 💡 **Tip**: Examples include automatic dependency checking and will show helpful installation instructions if
> dependencies are missing.

## Examples

For comprehensive examples demonstrating all features, see the [`examples/`](examples/) directory:

```bash
# Quick test - run a simple example
uv run python examples/basic_one_way_search.py

# Run all examples to explore different features
uv run python examples/round_trip_search.py
uv run python examples/complex_flight_search.py
uv run python examples/price_tracking.py
```

**Example Categories:**

* **Basic Usage**: One-way, round-trip, date searches
* **Advanced Filtering**: Time restrictions, airlines, seat classes
* **Data Analysis**: Price tracking, result processing with pandas
* **Error Handling**: Retry logic, robust error management
* **Complex Scenarios**: Multi-passenger, validation, business rules

Each example is self-contained and includes automatic dependency checking with helpful installation instructions.

## Development

```bash
# Clone the repository
git clone git@github.com:jacob7choi-xyz/fli-tracker.git
cd fli-tracker

# Install dependencies with uv
uv sync --all-extras

# Run tests
uv run pytest

# Run linting
uv run ruff check .
uv run ruff format .

# Build documentation
uv run mkdocs serve

# Or use the Makefile for common tasks
make install-all  # Install all dependencies
make test         # Run tests
make lint         # Check code style
make format       # Format code
```

### Docker Development

```bash
# Build the devcontainer
docker build -t fli-dev -f .devcontainer/Dockerfile .

# Run CI inside the container
docker run --rm fli-dev make lint test-all

# Or run lint and tests separately
docker run --rm fli-dev make lint
docker run --rm fli-dev make test-all
```

### Running CI Locally with act

To run GitHub Actions locally, install [act](https://github.com/nektos/act):

```bash
brew install act

# Run CI locally (lint + deterministic tests on Python 3.12)
make ci

# Or run CI inside Docker (no local act installation needed)
make ci-docker
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License. See [LICENSE.txt](LICENSE.txt) for details.

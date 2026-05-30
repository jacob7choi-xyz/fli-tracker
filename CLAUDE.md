# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fli is a Python library that provides programmatic access to Google Flights data through direct API interaction (reverse engineering). The project consists of:

- **CLI interface** (`fli/cli/`) - Typer-based command line tool with `flights` and `dates` commands
- **MCP server** (`fli/mcp/`) - Model Context Protocol server for AI assistant integration
- **Core utilities** (`fli/core/`) - Shared parsing and building utilities
- **Search engine** (`fli/search/`) - Flight and date search implementations using Google Flights API
- **Data models** (`fli/models/`) - Pydantic models for airports, airlines, and flight data structures
- **Price tracker** (`fli/tracker/`) - Route monitoring, price history, deal scoring, and digest email alerts

## Development Commands

### Core Development Tasks
```bash
# Install dependencies
uv sync --all-extras

# Run tests (use these specific commands)
make test                    # Standard test suite
make test-fuzz              # Run fuzzing tests (pytest -vv --fuzz)
make test-all               # Run all tests (pytest -vv --all)
uv run pytest -vv           # Alternative direct command

# Code quality
make lint                   # Check code with ruff
make lint-fix              # Auto-fix linting issues
make format                 # Format code with ruff
uv run ruff check .         # Direct ruff check
uv run ruff format .        # Direct ruff format

# MCP server
fli-mcp                     # Run MCP server on STDIO
fli-mcp-http               # Run MCP server over HTTP

# Documentation
make docs                   # Build MkDocs documentation
uv run mkdocs serve         # Serve docs locally
uv run mkdocs build         # Build static docs
```

### Test Configuration
- Tests use pytest with custom markers: `fuzz` (requires `--fuzz` flag) and `parallel` (for pytest-xdist)
- Test structure mirrors source code: `tests/cli/`, `tests/models/`, `tests/search/`, `tests/mcp/`
- Fuzzing tests are available but gated behind `--fuzz` flag

## Architecture Overview

### Core Components

1. **Core Layer** (`fli/core/`)
   - `parsers.py`: Shared parsing utilities (airports, airlines, stops, cabin class, time ranges)
   - `builders.py`: Filter building utilities (flight segments, time restrictions)
   - Used by both CLI and MCP for consistent parameter handling

2. **Client Layer** (`fli/search/client.py`)
   - Rate-limited HTTP client (10 req/sec) using curl-cffi for browser impersonation
   - Automatic retries with exponential backoff
   - Session management for Google Flights API communication

3. **Search Engine** (`fli/search/`)
   - `SearchFlights`: Core flight search using Google Flights API
   - `SearchDates`: Find cheapest dates within date ranges
   - Direct API integration (no web scraping)

4. **Data Models** (`fli/models/`)
   - **Base models**: `Airport`, `Airline` enums with IATA codes
   - **Google Flights models**: `FlightSearchFilters`, `FlightResult`, `FlightLeg`, etc.
   - **Filter models**: `TimeRestrictions`, `MaxStops`, `SeatType`, `SortBy`
   - All models use Pydantic for validation

5. **MCP Server** (`fli/mcp/`)
   - FastMCP-based server with two tools: `search_flights` and `search_dates`
   - Industry-standard parameter naming: `origin`, `destination`, `cabin_class`, `max_stops`
   - Prompt templates for guided searches
   - Configuration via environment variables

6. **CLI Interface** (`fli/cli/`)
   - Typer-based with two main commands: `flights` and `dates`
   - Smart argument parsing (treats non-command args as flights)
   - Rich console output for flight results

7. **Price Tracker** (`fli/tracker/`)
   - `models.py`: Route, Alert, PriceSnapshot, RouteStats models
   - `db.py`: SQLite storage with schema migrations, price history, route stats
   - `scanner.py`: Multi-duration route scanning with deduplication
   - `detector.py`: Price drop and threshold alert detection
   - `notifier.py`: Digest email formatting (Domestic/International sections), deal scoring, flight detail enrichment via API
   - `data/airport_locations.csv`: Airport metadata (city, region, country) for display labels and domestic/international classification
   - Per-route `max_price` caps filter noise by region ($120 domestic, $200 Mexico/Caribbean, $550 Europe, $750 Asia)
   - Flexible `durations` list per route (e.g., [5,7,10]) for multi-duration date searches

### Key Design Patterns

- **Direct API Access**: Uses reverse-engineered Google Flights API endpoints (not web scraping)
- **Rate Limiting**: Built-in 10 req/sec limit with automatic retry logic
- **Enum-Based Configuration**: Airports, airlines, seat types, etc. are strongly typed enums
- **Filter Pattern**: Search functionality uses comprehensive filter objects
- **Shared Utilities**: Core parsing/building logic shared between CLI and MCP
- **Validation**: Pydantic models ensure data integrity throughout

## Key Files and Entry Points

- `fli/cli/main.py` - CLI entry point and command registration
- `fli/mcp/server.py` - MCP server with `search_flights` and `search_dates` tools
- `fli/core/parsers.py` - Shared parsing utilities
- `fli/core/builders.py` - Shared filter building utilities
- `fli/search/flights.py` - Core flight search implementation
- `fli/search/client.py` - HTTP client with rate limiting and retries
- `fli/models/google_flights/` - All Google Flights data structures
- `fli/tracker/notifier.py` - Digest email formatting, deal scoring, flight detail enrichment
- `fli/tracker/scanner.py` - Multi-duration route scanning
- `fli/tracker/db.py` - SQLite storage with migrations
- `fli/tracker/detector.py` - Price drop and threshold alert detection
- `data/airport_locations.csv` - Airport city/country metadata for display and classification
- `pyproject.toml` - Package configuration with script entry points

## MCP Tool Reference

### `search_flights`
Search for flights on a specific date.

**Key Parameters:**
- `origin` / `destination` - Airport IATA codes
- `departure_date` / `return_date` - Dates in YYYY-MM-DD format
- `cabin_class` - ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST
- `max_stops` - ANY, NON_STOP, ONE_STOP, TWO_PLUS_STOPS
- `departure_window` - Time range in 'HH-HH' format
- `airlines` - List of airline IATA codes
- `sort_by` - CHEAPEST, DURATION, DEPARTURE_TIME, ARRIVAL_TIME

### `search_dates`
Find cheapest travel dates within a range.

**Key Parameters:**
- `origin` / `destination` - Airport IATA codes
- `start_date` / `end_date` - Date range in YYYY-MM-DD format
- `trip_duration` - Number of days for round trips
- `is_round_trip` - Boolean for round-trip search
- `cabin_class`, `max_stops`, `departure_window`, `airlines` - Same as above
- `sort_by_price` - Boolean to sort by price

## Code Style and Standards

- **Linting**: Uses Ruff with pycodestyle, pyflakes, isort, flake8-bugbear, and pydocstyle
- **Formatting**: Ruff formatter with 100 character line length, 4-space indentation
- **Type Hints**: Python 3.10+ with full type annotations
- **Docstrings**: Google-style docstrings (configured in mkdocs.yml)
- **Testing**: pytest with asyncio support and parallel execution capabilities

## Important Implementation Notes

- Google Flights API integration requires careful rate limiting (handled automatically)
- Airport and airline codes use official IATA standards
- Flight search supports complex filters: time ranges, cabin classes, stop preferences, sorting
- Date search finds cheapest flights within flexible date ranges
- MCP server uses industry-standard naming: `origin`/`destination`, `cabin_class`, `max_stops`
- Core utilities ensure consistent parsing between CLI and MCP interfaces

---

## Instruction Precedence

When instructions conflict, follow this order:

1. **"Plan first"** -- show plan, wait for approval before any code
2. **"Fix it"** -- minimal safe fix, but still:
   - Write a failing test first (for bugs)
   - If touching >1 file, show 3-bullet plan first
3. **When in doubt** -- ask, do not guess

## Working Style

### Plan Before Execute
- Before any multi-file change, show the plan first
- Do not write code until the approach is approved
- If something goes sideways, STOP. Go back to plan mode and re-plan

### Prove Your Work
- After completing a task, run tests to verify
- When asked to "prove it works", diff behavior and show evidence
- Commit after each completed phase with descriptive messages

### When Stuck
- Do not spin for more than 2 attempts on the same approach
- Ask for clarification instead of guessing
- Suggest 2-3 alternatives and let the user pick

### Teach, Don't Just Do
- Prioritize teaching over speed; explain the why behind changes
- When changes are accepted without questioning, push back and ask if the
  tradeoff is understood
- Resist vibe coding; if the user is rubber-stamping, slow down

### Use Subagents
- For complex refactors or exploration, use subagents to parallelize
- Keep main context clean; offload individual tasks to subagents

## Prompting Patterns

| When I say... | Do this |
|---------------|---------|
| "Plan first" | Show detailed plan, wait for approval |
| "Grill me on these changes" | Review critically, block until concerns addressed |
| "Prove it works" | Diff behavior, run tests, show evidence |
| "Now do it elegantly" | Scrap the quick fix, implement the clean solution |
| "Use subagents" | Parallelize with multiple agents |
| "Fix it" | Just fix it (but follow precedence rules above) |

## Bug Workflow

When a bug is reported:

1. **DON'T** start by trying to fix it
2. First, write a test that reproduces the bug
3. Then fix it
4. Prove the fix with a passing test

## Mistake Learning

After every correction:
- Ask: "What rule would have prevented this?"
- Add that rule to the Learned Rules section of this file
- Goal: mistake rate drops over time as CLAUDE.md improves

## Flight Search Output Standards

- **Always show full details.** When searching for flights, always include:
  departure date, return date, departure time, arrival time, duration, number
  of stops, airline, plane type, and price. Never show just price and
  destination.
- **Sweep with subagents by week per month.** When searching for cheapest
  flights, use subagents to parallelize the search. Each subagent handles
  one month, sweeping week-by-week intervals (e.g., 1st-8th, 8th-15th,
  15th-22nd, 22nd-29th). Run multiple month-agents in parallel. Each agent
  returns the cheapest result per destination for its month. The main
  context then aggregates across months and shows the overall cheapest.
- **Round-trip by default.** Unless told otherwise, search round-trip.
- **No lazy summaries.** Show the actual data in a readable table. Do not
  summarize or omit fields to save space.
- **State methodology.** Always state which months and date intervals were
  searched so the user knows the scope.

## Logging Levels

- **DEBUG**: Detailed diagnostics, function entry/exit, intermediate values
- **INFO**: General program flow, successful operations
- **WARNING**: Unexpected but recoverable situations where the operation still
  works as intended (e.g., a fallback was used, an optional feature is absent)
- **ERROR**: A requested capability is broken or lost, even if execution
  continues with degraded functionality. Litmus test: "did the thing the user
  asked for actually happen?" If no, that is ERROR, not WARNING.
- **CRITICAL**: Severe errors that may cause application shutdown

## DRY with Nuance

Extract helpers when duplicated logic encodes an invariant, cap, validation
rule, or error-handling pattern that should stay identical across call sites.
Do NOT extract helpers for trivial duplication if it makes the code less
direct. Three similar lines of code is better than a premature abstraction.

---

## Learned Rules

### Workflow
- **Always use uv.** Never use pip directly for any dependency management.
- **Confirm before commit.** Always show staged files and commit message for
  approval first.
- **Plan before implement.** Always enter plan mode before non-trivial changes.
- **No file overlap across branches.** Never commit the same file on two
  different PR branches.
- **Small focused commits.** Never one giant commit. Break work into small,
  focused commits with clear purpose.
- **Always dry-run first.** Before any real pipeline or destructive script
  execution, do a dry run.
- **Check before branch switch.** Always run git status before switching
  branches.

### Code
- **No em dashes, en dashes, or Unicode arrows.** Use commas, semicolons,
  parentheses, or split into two sentences instead. Use `->` instead of
  Unicode arrows. Hyphens for compound words are fine.
- **No emojis.** Never use emojis in code, comments, commits, PRs, or
  documentation.
- **No AI attribution.** Never include Co-Authored-By, "Generated with
  Claude Code", or any AI attribution in commits, PRs, or code comments.
- **No inference as fact.** Never state inferences as fact; only claim what
  code, docs, or the user explicitly states.
- **No laziness.** No shorthand like "etc." in public-facing output (PR
  comments, commits, docs). Verify claims in the code before stating them;
  never dress up a guess as a fact. Precision over speed.
- **Dead code: flag, don't delete.** Always proactively check for dead code
  during reviews and refactors. Flag findings with file:line citations, but
  never remove without explicit user approval. Dead code may be intentional
  scaffolding or in-progress work.
- **Tests mandatory and parallel.** Every addition or change must include
  corresponding tests written in parallel, not deferred. No "we'll add
  tests later."
- **Use pytest.parametrize.** Prefer `@pytest.mark.parametrize` for
  table-driven tests to reduce boilerplate and make test cases explicit.
- **Use StrEnum.** Prefer `StrEnum` (Python 3.11+) over the `(str, Enum)`
  mixin pattern for string enums.
- **Specific exception catches.** Catch specific exception types, not bare
  `except Exception`. Broad catches are acceptable only at external API
  boundaries (network calls, third-party libraries) where many exception
  types are possible.
- **Update CLI guards when extending enums/literals.** When a new value is
  added to a `Literal` type or enum (e.g., `RouteGroup`), search for every
  hardcoded allowlist that validates that type in CLI commands and update
  them in the same commit. The type system does not catch string allowlists.
- **Don't validate scoring weights without labeled data.** Validating a
  composite scorer requires ground truth: which outputs the user actually
  acted on. Features (z-score, percentile, lead-time) can be validated
  against the distribution. Weights between components cannot be validated
  without a labeled dataset. Never present hand-picked weights as empirically
  validated. Flag this distinction explicitly when building scoring systems.
- **Cache DB lookups before render loops.** When rendering a list of items
  that each require an expensive lookup keyed by ID (e.g., route stats per
  trigger), build a cache dict before the loop. Never call the same query
  inside a render loop -- that is an N+1 pattern that scales with list size.
- **SQL schema order matters.** In executescript() schemas, indexes must come
  after the tables they reference. If a table is created via migration (ALTER
  TABLE), its indexes must also go in _migrate(), not in the base SCHEMA_SQL.
- **Parameterize all SQL, even when safe.** Never use f-string or string
  interpolation in SQL queries, even when the interpolated value is safe
  (e.g., cast to int). Use parameterized queries exclusively. The pattern is
  wrong regardless of whether injection is actually possible.
- **html.escape() all variable data in HTML templates.** When building HTML
  strings, apply html.escape() to every variable -- city names from CSV,
  labels from external APIs, any string not hardcoded in the source. Apply
  it consistently: if trend is escaped and orig_city is not, that is a bug
  waiting to happen even if the current data source is trusted.

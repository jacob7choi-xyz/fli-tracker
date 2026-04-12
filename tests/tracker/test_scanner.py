"""Tests for the scanner module.

SearchDates is mocked to avoid hitting Google's API. The tests verify
that scan_route correctly converts Route config into DateSearchFilters,
transforms DatePrice results into PriceSnapshot objects, and that
sweep orchestrates scanning and storage correctly.
"""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fli.search.dates import DatePrice
from fli.tracker.db import TrackerDB
from fli.tracker.models import PriceSnapshot, Route
from fli.tracker.scanner import scan_route, sweep


@pytest.fixture()
def db(tmp_path: Path) -> TrackerDB:
    return TrackerDB(db_path=tmp_path / "test.db")


def _make_route(**kwargs) -> Route:
    """Create a route with defaults and an assigned id."""
    defaults = {
        "id": 1,
        "origin": "DFW",
        "destination": "FCO",
        "cabin_class": "ECONOMY",
        "max_stops": "ANY",
        "durations": [7],
        "look_ahead": 30,
        "is_round_trip": True,
    }
    defaults.update(kwargs)
    return Route(**defaults)


def _make_date_prices(count: int = 3, round_trip: bool = True) -> list[DatePrice]:
    """Generate sample DatePrice results."""
    base = datetime.now().date() + timedelta(days=10)
    prices = []
    for i in range(count):
        dep = datetime(base.year, base.month, base.day) + timedelta(days=i)
        if round_trip:
            ret = dep + timedelta(days=7)
            date_tuple = (dep, ret)
        else:
            date_tuple = (dep,)
        prices.append(DatePrice(date=date_tuple, price=400.0 + i * 50, currency="USD"))
    return prices


# ------------------------------------------------------------------
# scan_route
# ------------------------------------------------------------------


class TestScanRoute:
    """Tests for scan_route function."""

    def test_route_must_have_id(self):
        route = Route(origin="DFW", destination="FCO", id=None)
        with pytest.raises(ValueError, match="assigned id"):
            scan_route(route)

    @patch("fli.tracker.scanner.SearchDates")
    def test_returns_snapshots_from_search_results(self, mock_search_cls):
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = _make_date_prices(3, round_trip=True)

        route = _make_route()
        snapshots = scan_route(route)

        assert len(snapshots) == 3
        assert all(isinstance(s, PriceSnapshot) for s in snapshots)
        assert all(s.route_id == 1 for s in snapshots)
        assert all(s.currency == "USD" for s in snapshots)
        assert snapshots[0].price == 400.0
        assert snapshots[1].price == 450.0
        assert snapshots[2].price == 500.0

    @patch("fli.tracker.scanner.SearchDates")
    def test_round_trip_includes_return_date(self, mock_search_cls):
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = _make_date_prices(1, round_trip=True)

        route = _make_route(is_round_trip=True)
        snapshots = scan_route(route)

        assert len(snapshots) == 1
        assert snapshots[0].return_date is not None

    @patch("fli.tracker.scanner.SearchDates")
    def test_one_way_has_no_return_date(self, mock_search_cls):
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = _make_date_prices(1, round_trip=False)

        route = _make_route(is_round_trip=False)
        snapshots = scan_route(route)

        assert len(snapshots) == 1
        assert snapshots[0].return_date is None

    @patch("fli.tracker.scanner.SearchDates")
    def test_returns_empty_on_no_results(self, mock_search_cls):
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = None

        route = _make_route()
        snapshots = scan_route(route)

        assert snapshots == []

    @patch("fli.tracker.scanner.SearchDates")
    def test_returns_empty_on_exception(self, mock_search_cls):
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.side_effect = Exception("API error")

        route = _make_route()
        snapshots = scan_route(route)

        assert snapshots == []

    @patch("fli.tracker.scanner.SearchDates")
    def test_uses_route_cabin_class(self, mock_search_cls):
        """Verify that the cabin class from the route flows into the search filters."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = []

        route = _make_route(cabin_class="BUSINESS")
        scan_route(route)

        call_args = mock_instance.search.call_args
        filters = call_args[0][0]
        assert filters.seat_type.name == "BUSINESS"

    @patch("fli.tracker.scanner.SearchDates")
    def test_uses_route_max_stops(self, mock_search_cls):
        """Verify that max_stops from the route flows into the search filters."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = []

        route = _make_route(max_stops="NON_STOP")
        scan_route(route)

        call_args = mock_instance.search.call_args
        filters = call_args[0][0]
        assert filters.stops.name == "NON_STOP"

    @patch("fli.tracker.scanner.SearchDates")
    def test_currency_defaults_to_usd_when_none(self, mock_search_cls):
        """If SearchDates returns None currency, snapshot defaults to USD."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        base = datetime.now() + timedelta(days=10)
        mock_instance.search.return_value = [DatePrice(date=(base,), price=300.0, currency=None)]

        route = _make_route(is_round_trip=False)
        snapshots = scan_route(route)

        assert snapshots[0].currency == "USD"

    @patch("fli.tracker.scanner.SearchDates")
    def test_date_range_uses_look_ahead(self, mock_search_cls):
        """Verify that from_date and to_date span the route's look_ahead days."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = []

        route = _make_route(look_ahead=45)
        scan_route(route)

        call_args = mock_instance.search.call_args
        filters = call_args[0][0]
        from_date = datetime.strptime(filters.from_date, "%Y-%m-%d").date()
        to_date = datetime.strptime(filters.to_date, "%Y-%m-%d").date()
        assert (to_date - from_date).days == 45

    @patch("fli.tracker.scanner.SearchDates")
    def test_multi_duration_searches_each(self, mock_search_cls):
        """Multiple durations trigger one search per duration."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = _make_date_prices(1, round_trip=True)

        route = _make_route(durations=[5, 7, 10])
        scan_route(route)

        assert mock_instance.search.call_count == 3
        # Each call should use a different duration
        durations_used = []
        for call in mock_instance.search.call_args_list:
            filters = call[0][0]
            durations_used.append(filters.duration)
        assert sorted(durations_used) == [5, 7, 10]

    @patch("fli.tracker.scanner.SearchDates")
    def test_multi_duration_dedup_by_departure_return_price(self, mock_search_cls):
        """Identical (departure, return, price) across durations is deduplicated."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance

        base = datetime.now() + timedelta(days=10)
        dep = datetime(base.year, base.month, base.day)
        ret = dep + timedelta(days=7)
        same_result = [DatePrice(date=(dep, ret), price=400.0, currency="USD")]
        mock_instance.search.return_value = same_result

        route = _make_route(durations=[5, 7, 10])
        snapshots = scan_route(route)

        # Same snapshot returned by all 3 searches, but deduplicated to 1
        assert len(snapshots) == 1

    @patch("fli.tracker.scanner.SearchDates")
    def test_multi_duration_keeps_different_return_dates(self, mock_search_cls):
        """Different return dates from different durations are kept as distinct."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance

        base = datetime.now() + timedelta(days=10)
        dep = datetime(base.year, base.month, base.day)

        call_count = [0]

        def search_side_effect(filters):
            ret = dep + timedelta(days=filters.duration)
            call_count[0] += 1
            return [DatePrice(date=(dep, ret), price=400.0, currency="USD")]

        mock_instance.search.side_effect = search_side_effect

        route = _make_route(durations=[5, 7, 10])
        snapshots = scan_route(route)

        # Same departure, same price, but different return dates -> 3 distinct
        assert len(snapshots) == 3
        return_dates = {s.return_date for s in snapshots}
        assert len(return_dates) == 3

    @patch("fli.tracker.scanner.SearchDates")
    def test_one_way_ignores_extra_durations(self, mock_search_cls):
        """One-way routes only search once regardless of durations list."""
        mock_instance = MagicMock()
        mock_search_cls.return_value = mock_instance
        mock_instance.search.return_value = _make_date_prices(2, round_trip=False)

        route = _make_route(durations=[5, 7, 10], is_round_trip=False)
        snapshots = scan_route(route)

        assert mock_instance.search.call_count == 1
        assert len(snapshots) == 2


# ------------------------------------------------------------------
# sweep
# ------------------------------------------------------------------


class TestSweep:
    """Tests for the sweep function."""

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_scans_active_routes(self, mock_scan, db: TrackerDB):
        db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_route(Route(origin="JFK", destination="LHR"))
        db.add_route(Route(origin="LAX", destination="NRT", active=False))

        mock_scan.return_value = [
            PriceSnapshot(route_id=1, departure_date="2026-07-15", price=500.0, currency="USD")
        ]

        total = sweep(db)

        assert mock_scan.call_count == 2
        assert total == 2

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_stores_snapshots(self, mock_scan, db: TrackerDB):
        route = db.add_route(Route(origin="DFW", destination="FCO"))

        mock_scan.return_value = [
            PriceSnapshot(
                route_id=route.id, departure_date="2026-07-15", price=500.0, currency="USD"
            ),
            PriceSnapshot(
                route_id=route.id, departure_date="2026-07-16", price=450.0, currency="USD"
            ),
        ]

        total = sweep(db)

        assert total == 2
        stored = db.get_snapshots(route.id)
        assert len(stored) == 2

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_no_active_routes(self, mock_scan, db: TrackerDB):
        total = sweep(db)
        assert total == 0
        mock_scan.assert_not_called()

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_handles_empty_scan(self, mock_scan, db: TrackerDB):
        db.add_route(Route(origin="DFW", destination="FCO"))
        mock_scan.return_value = []

        total = sweep(db)
        assert total == 0

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_continues_on_partial_failure(self, mock_scan, db: TrackerDB):
        """If one route returns empty, other routes still get processed."""
        route1 = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_route(Route(origin="JFK", destination="LHR"))

        def side_effect(route):
            if route.id == route1.id:
                return []
            return [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=600.0, currency="USD"
                )
            ]

        mock_scan.side_effect = side_effect

        total = sweep(db)
        assert total == 1
        assert mock_scan.call_count == 2

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_filters_by_group(self, mock_scan, db: TrackerDB):
        """When group is specified, only matching routes are scanned."""
        db.add_route(Route(origin="DFW", destination="ORD"))   # domestic
        db.add_route(Route(origin="DFW", destination="FCO"))   # longhaul

        mock_scan.return_value = [
            PriceSnapshot(route_id=1, departure_date="2026-07-15", price=100.0, currency="USD")
        ]

        sweep(db, group="domestic")
        assert mock_scan.call_count == 1
        scanned_route = mock_scan.call_args[0][0]
        assert scanned_route.destination == "ORD"

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_longhaul_group(self, mock_scan, db: TrackerDB):
        """Longhaul group scans only international routes."""
        db.add_route(Route(origin="DFW", destination="ORD"))   # domestic
        db.add_route(Route(origin="DFW", destination="FCO"))   # longhaul

        mock_scan.return_value = [
            PriceSnapshot(route_id=1, departure_date="2026-07-15", price=500.0, currency="USD")
        ]

        sweep(db, group="longhaul")
        assert mock_scan.call_count == 1
        scanned_route = mock_scan.call_args[0][0]
        assert scanned_route.destination == "FCO"

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_no_group_scans_all(self, mock_scan, db: TrackerDB):
        """Without group filter, all active routes are scanned."""
        db.add_route(Route(origin="DFW", destination="ORD"))
        db.add_route(Route(origin="DFW", destination="FCO"))

        mock_scan.return_value = []

        sweep(db, group=None)
        assert mock_scan.call_count == 2

    @patch("fli.tracker.scanner.scan_route")
    def test_sweep_continues_on_scan_exception(self, mock_scan, db: TrackerDB):
        """If scan_route raises an exception, sweep catches it and continues."""
        route1 = db.add_route(Route(origin="DFW", destination="FCO"))
        db.add_route(Route(origin="JFK", destination="LHR"))

        def side_effect(route):
            if route.id == route1.id:
                raise RuntimeError("API timeout")
            return [
                PriceSnapshot(
                    route_id=route.id, departure_date="2026-07-15", price=600.0, currency="USD"
                )
            ]

        mock_scan.side_effect = side_effect

        total = sweep(db)
        assert total == 1
        assert mock_scan.call_count == 2

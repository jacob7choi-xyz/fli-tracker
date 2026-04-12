"""Tests for route region classification."""

import pytest

from fli.tracker.regions import _AIRPORT_COUNTRIES, route_group


class TestRouteGroup:
    """Tests for route_group classification."""

    @pytest.mark.parametrize(
        ("origin", "destination", "expected"),
        [
            pytest.param("DFW", "ORD", "domestic", id="us_to_us"),
            pytest.param("DFW", "SJU", "domestic", id="us_to_puerto_rico"),
            pytest.param("DFW", "CUN", "domestic", id="us_to_mexico"),
            pytest.param("DFW", "SJO", "domestic", id="us_to_costa_rica"),
            pytest.param("DFW", "SDQ", "domestic", id="us_to_dominican_republic"),
            pytest.param("DFW", "GUA", "domestic", id="us_to_guatemala"),
            pytest.param("DFW", "SAL", "domestic", id="us_to_el_salvador"),
            pytest.param("DFW", "MBJ", "domestic", id="us_to_jamaica"),
            pytest.param("DFW", "YVR", "domestic", id="us_to_canada"),
            pytest.param("DFW", "FCO", "longhaul", id="us_to_italy"),
            pytest.param("DFW", "NRT", "longhaul", id="us_to_japan"),
            pytest.param("DFW", "SYD", "longhaul", id="us_to_australia"),
            pytest.param("DFW", "BOG", "longhaul", id="us_to_colombia"),
            pytest.param("DFW", "LHR", "longhaul", id="us_to_uk"),
            pytest.param("DFW", "ZZZ", "longhaul", id="unknown_destination"),
        ],
    )
    def test_classification(self, origin, destination, expected):
        assert route_group(origin, destination) == expected


class TestAirportCountries:
    """Tests for airport country data loading."""

    def test_loads_countries(self):
        assert len(_AIRPORT_COUNTRIES) > 0

    def test_us_airports(self):
        assert _AIRPORT_COUNTRIES["DFW"] == "US"
        assert _AIRPORT_COUNTRIES["ORD"] == "US"
        assert _AIRPORT_COUNTRIES["JFK"] == "US"

    def test_international_airports(self):
        assert _AIRPORT_COUNTRIES["FCO"] == "Italy"
        assert _AIRPORT_COUNTRIES["NRT"] == "Japan"
        assert _AIRPORT_COUNTRIES["CUN"] == "Mexico"

    def test_near_international(self):
        assert _AIRPORT_COUNTRIES["SJU"] == "Puerto Rico"
        assert _AIRPORT_COUNTRIES["YVR"] == "Canada"

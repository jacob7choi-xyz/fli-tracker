import pytest
from pydantic import ValidationError

from fli.tracker.models import Alert, AlertType, NotificationRecord, PriceSnapshot, Route


class TestRoute:
    """Tests for the Route model."""

    def test_defaults(self):
        route = Route(origin="DFW", destination="FCO")
        assert route.cabin_class == "ECONOMY"
        assert route.max_stops == "ANY"
        assert route.durations == [7]
        assert route.trip_duration == 7  # backward compat property
        assert route.look_ahead == 45
        assert route.is_round_trip is True
        assert route.max_price is None
        assert route.active is True
        assert route.id is None
        assert route.created_at is None

    def test_custom_values(self):
        route = Route(
            origin="JFK",
            destination="LHR",
            cabin_class="BUSINESS",
            max_stops="NON_STOP",
            durations=[5, 7, 10],
            look_ahead=120,
            is_round_trip=False,
            max_price=800.0,
        )
        assert route.origin == "JFK"
        assert route.destination == "LHR"
        assert route.cabin_class == "BUSINESS"
        assert route.max_stops == "NON_STOP"
        assert route.durations == [5, 7, 10]
        assert route.trip_duration == 5  # first duration
        assert route.look_ahead == 120
        assert route.is_round_trip is False
        assert route.max_price == 800.0

    def test_durations_must_be_positive(self):
        with pytest.raises(ValidationError):
            Route(origin="DFW", destination="FCO", durations=[0])

    def test_durations_cannot_be_empty(self):
        with pytest.raises(ValidationError):
            Route(origin="DFW", destination="FCO", durations=[])

    def test_max_price_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            Route(origin="DFW", destination="FCO", max_price=-1.0)

    def test_max_price_zero_is_valid(self):
        route = Route(origin="DFW", destination="FCO", max_price=0.0)
        assert route.max_price == 0.0

    def test_look_ahead_must_be_positive(self):
        with pytest.raises(ValidationError):
            Route(origin="DFW", destination="FCO", look_ahead=0)


class TestPriceSnapshot:
    """Tests for the PriceSnapshot model."""

    def test_required_fields(self):
        snap = PriceSnapshot(
            route_id=1,
            departure_date="2026-07-15",
            price=487.0,
            currency="USD",
        )
        assert snap.route_id == 1
        assert snap.departure_date == "2026-07-15"
        assert snap.return_date is None
        assert snap.price == 487.0
        assert snap.currency == "USD"

    def test_with_return_date(self):
        snap = PriceSnapshot(
            route_id=1,
            departure_date="2026-07-15",
            return_date="2026-07-22",
            price=523.0,
            currency="USD",
        )
        assert snap.return_date == "2026-07-22"

    def test_price_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            PriceSnapshot(
                route_id=1,
                departure_date="2026-07-15",
                price=-1.0,
                currency="USD",
            )


class TestAlert:
    """Tests for the Alert model."""

    def test_threshold_alert(self):
        alert = Alert(
            route_id=1,
            alert_type=AlertType.THRESHOLD,
            threshold=500.0,
            notify_url="tgram://bot/chat",
        )
        assert alert.alert_type == AlertType.THRESHOLD
        assert alert.threshold == 500.0
        assert alert.active is True

    def test_drop_alert(self):
        alert = Alert(
            route_id=1,
            alert_type=AlertType.DROP,
            notify_url="discord://webhook/token",
        )
        assert alert.alert_type == AlertType.DROP
        assert alert.threshold is None

    def test_alert_type_string_coercion(self):
        alert = Alert(
            route_id=1,
            alert_type="threshold",
            notify_url="mailto://user@example.com",
        )
        assert alert.alert_type == AlertType.THRESHOLD


class TestNotificationRecord:
    """Tests for the NotificationRecord model."""

    def test_required_fields(self):
        record = NotificationRecord(
            alert_id=1,
            price=487.0,
            message="Price drop: DFW -> FCO",
        )
        assert record.alert_id == 1
        assert record.price == 487.0
        assert record.message == "Price drop: DFW -> FCO"
        assert record.sent_at is None

    def test_price_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            NotificationRecord(alert_id=1, price=-10.0, message="test")

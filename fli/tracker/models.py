"""Pydantic models for the tracker layer.

These models represent tracked routes, price snapshots, alerts,
and notification log entries. They are the in-memory representation
of rows in the SQLite database.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, NonNegativeFloat, PositiveInt


class AlertType(StrEnum):
    """Type of price alert trigger."""

    THRESHOLD = "threshold"
    DROP = "drop"


class Route(BaseModel):
    """A watched flight route."""

    id: int | None = None
    origin: str
    destination: str
    cabin_class: str = "ECONOMY"
    max_stops: str = "ANY"
    trip_duration: PositiveInt = 7
    look_ahead: PositiveInt = 90
    is_round_trip: bool = True
    created_at: datetime | None = None
    active: bool = True


class PriceSnapshot(BaseModel):
    """A single observed price for a route on a departure date."""

    id: int | None = None
    route_id: int
    departure_date: str
    return_date: str | None = None
    price: NonNegativeFloat
    currency: str
    scanned_at: datetime | None = None


class Alert(BaseModel):
    """An alert configuration attached to a route."""

    id: int | None = None
    route_id: int
    alert_type: AlertType
    threshold: float | None = None
    notify_url: str
    active: bool = True
    created_at: datetime | None = None


class NotificationRecord(BaseModel):
    """A log entry for a sent notification."""

    id: int | None = None
    alert_id: int
    departure_date: str | None = None
    return_date: str | None = None
    price: NonNegativeFloat
    message: str
    sent_at: datetime | None = None


class MonthlyStats(BaseModel):
    """Price statistics for a single month on a route."""

    avg_price: float
    count: int


class RouteStats(BaseModel):
    """Aggregate price statistics for a tracked route.

    Used by the deal scorer to evaluate how a new fare compares
    to the route's historical price distribution.
    """

    all_time_min: float
    overall_median: float
    total_count: int
    days_of_history: int
    monthly: dict[int, MonthlyStats]

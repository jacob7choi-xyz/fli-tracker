"""Pydantic models for the tracker layer.

These models represent tracked routes, price snapshots, alerts,
and notification log entries. They are the in-memory representation
of rows in the SQLite database.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from annotated_types import MinLen
from pydantic import BaseModel, Field, NonNegativeFloat, PositiveInt


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
    durations: Annotated[list[PositiveInt], MinLen(1)] = [7]
    look_ahead: PositiveInt = 45
    is_round_trip: bool = True
    max_price: NonNegativeFloat | None = None
    must_buy_price: NonNegativeFloat | None = None
    created_at: datetime | None = None
    active: bool = True
    snoozed_until: str | None = None

    @property
    def trip_duration(self) -> int:
        """Return the first duration for backward compatibility."""
        return self.durations[0]


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
    price_mean: float = 0.0
    price_stddev: float | None = None
    volatility_14d: float | None = None
    lead_time_buckets: dict[str, float] = Field(default_factory=dict)
    price_percentiles: dict[int, float] = Field(default_factory=dict)

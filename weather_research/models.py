from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Side = Literal["yes", "no"]


@dataclass(frozen=True)
class IncentiveProgram:
    market_ticker: str
    program_type: str
    reward_cents: int
    target_size: int
    discount_factor: float
    starts_at: datetime | None = None
    ends_at: datetime | None = None


@dataclass(frozen=True)
class WeatherRule:
    series_ticker: str
    station_id: str
    timezone: str
    observation_type: Literal["daily_high", "daily_low"]
    rounding: Literal["nearest_int", "floor", "ceil", "none"]
    revision_policy: str
    source_name: str
    time_basis: Literal["civil", "local_standard"] = "civil"
    standard_utc_offset_minutes: int | None = None

    def __post_init__(self) -> None:
        if self.time_basis == "local_standard" and self.standard_utc_offset_minutes is None:
            raise ValueError(
                "local_standard rules require standard_utc_offset_minutes; "
                "do not infer it from a DST-observing zone"
            )
        if self.time_basis == "civil" and self.standard_utc_offset_minutes is not None:
            raise ValueError(
                "standard_utc_offset_minutes is only valid with time_basis='local_standard'"
            )


@dataclass(frozen=True)
class BookTop:
    ticker: str
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    yes_bid_size: int = 0
    yes_ask_size: int = 0
    captured_at: datetime | None = None


@dataclass(frozen=True)
class ThresholdContract:
    ticker: str
    comparator: Literal[">=", ">", "<=", "<"]
    threshold: float
    event_date: date | None = None


@dataclass(frozen=True)
class BucketContract:
    ticker: str
    lower: float | None
    upper: float | None
    lower_inclusive: bool = True
    upper_inclusive: bool = False
    event_date: date | None = None


@dataclass(frozen=True)
class Signal:
    ticker: str
    kind: str
    side: Side
    executable_price_cents: int
    gross_gap_cents: int
    displayed_size: int
    observed_value: float
    reason: str

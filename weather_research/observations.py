from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import ceil, floor
from typing import Any
from zoneinfo import ZoneInfo

import httpx


@dataclass(frozen=True)
class StationObservation:
    station_id: str
    observed_at: datetime
    temperature_c: float

    @property
    def temperature_f(self) -> float:
        return self.temperature_c * 9 / 5 + 32


@dataclass(frozen=True)
class RoundingCandidates:
    """Lossless source plus CLI rounding hypotheses.

    cf: round(C -> F)
    c: F(round(C)); retained as a mechanism diagnostic even though it is not
       generally on the CLI whole-degree Fahrenheit output grid.
    cff: round(F(round(C))); integer-grid candidate for an ASOS Celsius-first
         quantization path followed by CLI Fahrenheit rounding.
    """

    temperature_c: float
    temperature_f_round_cf: float
    temperature_f_round_c: float
    temperature_f_round_cff: float


def _half_up_integer(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rounding_candidates(temperature_c: float) -> RoundingCandidates:
    raw_f = temperature_c * 9 / 5 + 32
    rounded_c = _half_up_integer(temperature_c)
    f_after_round_c = rounded_c * 9 / 5 + 32
    return RoundingCandidates(
        temperature_c=temperature_c,
        temperature_f_round_cf=_half_up_integer(raw_f),
        temperature_f_round_c=f_after_round_c,
        temperature_f_round_cff=_half_up_integer(f_after_round_c),
    )


def selected_temperature_f(temperature_c: float, rounding: str) -> float:
    """Select the configured settlement-path candidate from raw Celsius."""
    candidates = rounding_candidates(temperature_c)
    if rounding == "celsius_int_then_convert":
        return candidates.temperature_f_round_cff
    return apply_rule_rounding(temperature_c * 9 / 5 + 32, rounding)


@dataclass
class NWSObservationClient:
    base_url: str = "https://api.weather.gov"
    user_agent: str = "kalshi-weather-research/0.1 (research logger)"
    timeout: float = 15.0

    def latest(self, station_id: str) -> StationObservation:
        url = f"{self.base_url}/stations/{station_id}/observations/latest"
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = client.get(url)
            response.raise_for_status()
            data: dict[str, Any] = response.json()["properties"]
        return self._parse(station_id, data)

    def range(self, station_id: str, start: datetime, end: datetime) -> list[StationObservation]:
        """Fetch every available station observation in an inclusive UTC window."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        url = f"{self.base_url}/stations/{station_id}/observations"
        params = {
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "limit": 500,
        }
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            features = response.json().get("features", [])
        out: list[StationObservation] = []
        for feature in features:
            props = feature.get("properties", {})
            try:
                out.append(self._parse(station_id, props))
            except RuntimeError:
                continue
        return sorted(out, key=lambda row: row.observed_at)

    @staticmethod
    def _parse(station_id: str, data: dict[str, Any]) -> StationObservation:
        value = data.get("temperature", {}).get("value")
        if value is None:
            raise RuntimeError(f"station {station_id} returned no temperature")
        observed_at = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        return StationObservation(station_id, observed_at.astimezone(timezone.utc), float(value))


def climate_timezone(
    timezone_name: str,
    *,
    time_basis: str = "civil",
    standard_utc_offset_minutes: int | None = None,
):
    if time_basis == "civil":
        if standard_utc_offset_minutes is not None:
            raise ValueError("civil time cannot specify a fixed standard offset")
        return ZoneInfo(timezone_name)
    if time_basis == "local_standard":
        if standard_utc_offset_minutes is None:
            raise ValueError("local_standard time requires standard_utc_offset_minutes")
        return timezone(timedelta(minutes=standard_utc_offset_minutes), name=f"LST{standard_utc_offset_minutes:+d}")
    raise ValueError(f"unsupported time_basis: {time_basis}")


def climatological_date(
    observed_at: datetime,
    timezone_name: str,
    *,
    time_basis: str = "civil",
    standard_utc_offset_minutes: int | None = None,
):
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    zone = climate_timezone(
        timezone_name,
        time_basis=time_basis,
        standard_utc_offset_minutes=standard_utc_offset_minutes,
    )
    return observed_at.astimezone(zone).date()


def local_day_window(
    now: datetime,
    timezone_name: str,
    *,
    time_basis: str = "civil",
    standard_utc_offset_minutes: int | None = None,
) -> tuple[datetime, datetime]:
    """Return climate-day midnight through now as timezone-aware UTC datetimes."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = climate_timezone(
        timezone_name,
        time_basis=time_basis,
        standard_utc_offset_minutes=standard_utc_offset_minutes,
    )
    local_now = now.astimezone(zone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc), now.astimezone(timezone.utc)


def apply_rule_rounding(value: float, rounding: str) -> float:
    if rounding == "none":
        return value
    if rounding == "nearest_int":
        return _half_up_integer(value)
    if rounding == "floor":
        return float(floor(value))
    if rounding == "ceil":
        return float(ceil(value))
    if rounding == "celsius_int_then_convert":
        temperature_c = (value - 32) * 5 / 9
        rounded_c = _half_up_integer(temperature_c)
        return _half_up_integer(rounded_c * 9 / 5 + 32)
    raise ValueError(f"unsupported rounding rule: {rounding}")


def update_running_extreme(current: float | None, value: float, observation_type: str) -> float:
    if current is None:
        return value
    if observation_type == "daily_high":
        return max(current, value)
    if observation_type == "daily_low":
        return min(current, value)
    raise ValueError(f"unsupported observation_type: {observation_type}")


def extreme(values: list[float], observation_type: str) -> float:
    if not values:
        raise ValueError("values cannot be empty")
    if observation_type == "daily_high":
        return max(values)
    if observation_type == "daily_low":
        return min(values)
    raise ValueError(f"unsupported observation_type: {observation_type}")


def recompute_all_candidate_extremes(
    observations: list[StationObservation], observation_type: str
) -> tuple[float, float, float]:
    if not observations:
        raise ValueError("observations cannot be empty")
    candidates = [rounding_candidates(row.temperature_c) for row in observations]
    return (
        extreme([row.temperature_f_round_cf for row in candidates], observation_type),
        extreme([row.temperature_f_round_c for row in candidates], observation_type),
        extreme([row.temperature_f_round_cff for row in candidates], observation_type),
    )


def recompute_candidate_extremes(
    observations: list[StationObservation], observation_type: str
) -> tuple[float, float]:
    """Backward-compatible two-path view; new code should use the all-path helper."""
    cf, c, _ = recompute_all_candidate_extremes(observations, observation_type)
    return cf, c


def recompute_day_extreme(observations: list[StationObservation], observation_type: str, rounding: str) -> float:
    if not observations:
        raise ValueError("observations cannot be empty")
    values = [selected_temperature_f(row.temperature_c, rounding) for row in observations]
    return extreme(values, observation_type)

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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


def climatological_date(observed_at: datetime, timezone_name: str):
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return observed_at.astimezone(ZoneInfo(timezone_name)).date()


def local_day_window(now: datetime, timezone_name: str) -> tuple[datetime, datetime]:
    """Return local midnight through now as timezone-aware UTC datetimes."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc), now.astimezone(timezone.utc)


def apply_rule_rounding(value: float, rounding: str) -> float:
    if rounding == "none":
        return value
    if rounding == "nearest_int":
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if rounding == "floor":
        return float(floor(value))
    if rounding == "ceil":
        return float(ceil(value))
    raise ValueError(f"unsupported rounding rule: {rounding}")


def update_running_extreme(current: float | None, value: float, observation_type: str) -> float:
    if current is None:
        return value
    if observation_type == "daily_high":
        return max(current, value)
    if observation_type == "daily_low":
        return min(current, value)
    raise ValueError(f"unsupported observation_type: {observation_type}")


def recompute_day_extreme(observations: list[StationObservation], observation_type: str, rounding: str) -> float:
    if not observations:
        raise ValueError("observations cannot be empty")
    values = [apply_rule_rounding(row.temperature_f, rounding) for row in observations]
    if observation_type == "daily_high":
        return max(values)
    if observation_type == "daily_low":
        return min(values)
    raise ValueError(f"unsupported observation_type: {observation_type}")

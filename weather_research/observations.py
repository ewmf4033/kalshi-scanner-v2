from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

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
        value = data["temperature"]["value"]
        if value is None:
            raise RuntimeError(f"station {station_id} returned no temperature")
        observed_at = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        return StationObservation(station_id, observed_at.astimezone(timezone.utc), float(value))


def update_running_extreme(current: float | None, value: float, observation_type: str) -> float:
    if current is None:
        return value
    if observation_type == "daily_high":
        return max(current, value)
    if observation_type == "daily_low":
        return min(current, value)
    raise ValueError(f"unsupported observation_type: {observation_type}")

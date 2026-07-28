from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReconciliationStats:
    total: int
    errors: int

    @property
    def rate(self) -> float:
        return self.errors / self.total if self.total else 0.0

    def wilson_upper(self, confidence_z: float = 1.96) -> float:
        if self.total == 0:
            return 1.0
        n = self.total
        p = self.rate
        z2 = confidence_z**2
        center = p + z2 / (2 * n)
        radius = confidence_z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5)
        return min(1.0, (center + radius) / (1 + z2 / n))


def _tenths(value: float) -> int:
    """Normalize weather values to integer tenths before comparison."""
    return round(value * 10)


def compare_settlement(parsed_value: float, settled_value: float, tolerance_tenths: int = 0) -> bool:
    if tolerance_tenths < 0:
        raise ValueError("tolerance_tenths must be non-negative")
    return abs(_tenths(parsed_value) - _tenths(settled_value)) <= tolerance_tenths


@dataclass
class ReconciliationLedger:
    """Collect every station-day and keep signal-days separately identifiable."""

    rows: list[dict] = field(default_factory=list)

    def add(
        self,
        *,
        station_id: str,
        date: str,
        parsed_value: float,
        settled_value: float,
        signal_fired: bool,
        tolerance_tenths: int = 0,
    ) -> None:
        agreed = compare_settlement(parsed_value, settled_value, tolerance_tenths)
        self.rows.append(
            {
                "station_id": station_id,
                "date": date,
                "parsed_tenths": _tenths(parsed_value),
                "settled_tenths": _tenths(settled_value),
                "signal_fired": signal_fired,
                "agreed": agreed,
            }
        )

    def stats(self, *, signal_only: bool = False) -> ReconciliationStats:
        rows = [row for row in self.rows if row["signal_fired"]] if signal_only else self.rows
        return ReconciliationStats(total=len(rows), errors=sum(not row["agreed"] for row in rows))

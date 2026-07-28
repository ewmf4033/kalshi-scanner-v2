from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP


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


def _tenths(value: float | Decimal | int) -> int:
    """Normalize weather values with decimal half-up rounding, never banker's rounding."""
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((decimal_value * 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compare_settlement(
    parsed_value: float | Decimal | int,
    settled_value: float | Decimal | int,
    tolerance_tenths: int = 0,
) -> bool:
    if tolerance_tenths < 0:
        raise ValueError("tolerance_tenths must be non-negative")
    return abs(_tenths(parsed_value) - _tenths(settled_value)) <= tolerance_tenths


@dataclass
class ReconciliationLedger:
    """Collect every station-day and expose baseline, signal, and would-fill cohorts."""

    rows: list[dict] = field(default_factory=list)

    def add(
        self,
        *,
        station_id: str,
        date: str,
        parsed_value: float | Decimal | int,
        settled_value: float | Decimal | int,
        signal_fired: bool,
        displayed_depth: int = 0,
        quote_survival_seconds: float = 0.0,
        gross_gap_cents: float = 0.0,
        required_depth: int = 50,
        required_survival_seconds: float = 3.0,
        required_gap_cents: float = 0.0,
        tolerance_tenths: int = 0,
    ) -> None:
        agreed = compare_settlement(parsed_value, settled_value, tolerance_tenths)
        would_have_filled = (
            signal_fired
            and displayed_depth >= required_depth
            and quote_survival_seconds >= required_survival_seconds
            and gross_gap_cents >= required_gap_cents
        )
        self.rows.append(
            {
                "station_id": station_id,
                "date": date,
                "parsed_tenths": _tenths(parsed_value),
                "settled_tenths": _tenths(settled_value),
                "signal_fired": signal_fired,
                "displayed_depth": displayed_depth,
                "quote_survival_seconds": quote_survival_seconds,
                "gross_gap_cents": gross_gap_cents,
                "would_have_filled": would_have_filled,
                "agreed": agreed,
            }
        )

    def stats(
        self,
        *,
        signal_only: bool = False,
        fill_only: bool = False,
    ) -> ReconciliationStats:
        if signal_only and fill_only:
            raise ValueError("choose signal_only or fill_only, not both")
        if fill_only:
            rows = [row for row in self.rows if row["would_have_filled"]]
        elif signal_only:
            rows = [row for row in self.rows if row["signal_fired"]]
        else:
            rows = self.rows
        return ReconciliationStats(total=len(rows), errors=sum(not row["agreed"] for row in rows))

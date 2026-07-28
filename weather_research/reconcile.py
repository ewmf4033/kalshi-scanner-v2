from __future__ import annotations

from dataclasses import dataclass


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


def compare_settlement(parsed_value: float, settled_value: float, tolerance: float = 0.0) -> bool:
    return abs(parsed_value - settled_value) <= tolerance

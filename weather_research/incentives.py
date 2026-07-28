from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IncentiveEconomics:
    published_reward_cents: int
    measured_total_score: float
    proposed_score: float
    competitor_response_haircut: float = 0.25
    expected_fill_loss_cents: float = 0.0
    operating_cost_cents: float = 0.0

    def expected_reward_cents(self) -> float:
        if self.measured_total_score < 0 or self.proposed_score < 0:
            raise ValueError("scores cannot be negative")
        response = max(0.0, self.competitor_response_haircut)
        denominator = self.measured_total_score + self.proposed_score * (1 + response)
        if denominator == 0:
            return 0.0
        return self.published_reward_cents * self.proposed_score / denominator

    def expected_net_cents(self) -> float:
        return self.expected_reward_cents() - self.expected_fill_loss_cents - self.operating_cost_cents


def qualifying_score(size: int, distance_ticks: int, discount_factor: float) -> float:
    """Approximation until the live program formula is versioned alongside each run."""
    if size <= 0:
        return 0.0
    if distance_ticks < 0:
        raise ValueError("distance_ticks cannot be negative")
    return size * (discount_factor ** distance_ticks)

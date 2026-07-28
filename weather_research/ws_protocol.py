from __future__ import annotations

from dataclasses import dataclass, field


def orderbook_subscription(message_id: int, market_tickers: list[str]) -> dict:
    """Build the only accepted subscription shape for this experiment."""
    if not market_tickers:
        raise ValueError("market_tickers cannot be empty")
    return {
        "id": message_id,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": market_tickers,
            "use_yes_price": True,
        },
    }


class SequenceGapError(RuntimeError):
    pass


@dataclass
class SequenceTracker:
    """Reject deltas unless each subscription sequence is contiguous.

    A gap invalidates local book state; callers must discard it and request a
    fresh snapshot before accepting additional deltas.
    """

    last_by_subscription: dict[int, int] = field(default_factory=dict)
    invalid_subscriptions: set[int] = field(default_factory=set)

    def accept_snapshot(self, subscription_id: int, seq: int) -> None:
        self.last_by_subscription[subscription_id] = seq
        self.invalid_subscriptions.discard(subscription_id)

    def accept_delta(self, subscription_id: int, seq: int) -> None:
        if subscription_id in self.invalid_subscriptions:
            raise SequenceGapError(
                f"subscription {subscription_id} requires a fresh snapshot"
            )
        previous = self.last_by_subscription.get(subscription_id)
        if previous is None:
            self.invalid_subscriptions.add(subscription_id)
            raise SequenceGapError(
                f"delta received before snapshot for subscription {subscription_id}"
            )
        expected = previous + 1
        if seq != expected:
            self.invalid_subscriptions.add(subscription_id)
            raise SequenceGapError(
                f"sequence gap for subscription {subscription_id}: expected {expected}, got {seq}"
            )
        self.last_by_subscription[subscription_id] = seq

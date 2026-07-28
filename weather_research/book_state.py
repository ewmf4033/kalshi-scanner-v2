from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import BookTop
from .ws_protocol import SequenceGapError, SequenceTracker


@dataclass
class OrderBookState:
    """Sequence-safe top-of-book state from Kalshi unified-YES messages.

    A sequence failure clears every local book because stale state must never
    generate a signal. A fresh snapshot is required before deltas resume.
    """

    tracker: SequenceTracker = field(default_factory=SequenceTracker)
    books: dict[str, BookTop] = field(default_factory=dict)

    def apply(self, message: dict[str, Any]) -> BookTop | None:
        msg_type = message.get("type")
        sid = int(message.get("sid", message.get("subscription_id", 0)))
        seq = int(message["seq"])
        payload = message.get("msg", message.get("data", {}))
        ticker = payload.get("market_ticker") or payload.get("ticker")
        if not ticker:
            raise ValueError("order-book message missing market ticker")

        try:
            if msg_type in {"orderbook_snapshot", "snapshot"}:
                self.tracker.accept_snapshot(sid, seq)
                book = self._from_snapshot(ticker, payload)
            elif msg_type in {"orderbook_delta", "delta"}:
                self.tracker.accept_delta(sid, seq)
                previous = self.books.get(ticker)
                if previous is None:
                    raise SequenceGapError(f"delta received without ticker snapshot: {ticker}")
                book = self._from_delta(previous, payload)
            else:
                return None
        except SequenceGapError:
            self.books.clear()
            raise

        self.books[ticker] = book
        return book

    @staticmethod
    def _best(levels: list[list[int]] | None, *, highest: bool) -> tuple[int | None, int]:
        if not levels:
            return None, 0
        price, size = (max(levels) if highest else min(levels))
        return int(price), int(size)

    def _from_snapshot(self, ticker: str, payload: dict[str, Any]) -> BookTop:
        yes_bid, yes_bid_size = self._best(payload.get("yes") or payload.get("yes_bids"), highest=True)
        no_bid, no_bid_size = self._best(payload.get("no") or payload.get("no_bids"), highest=True)
        yes_ask = None if no_bid is None else 100 - no_bid
        return BookTop(
            ticker=ticker,
            yes_bid_cents=yes_bid,
            yes_ask_cents=yes_ask,
            yes_bid_size=yes_bid_size,
            yes_ask_size=no_bid_size,
            captured_at=datetime.now(timezone.utc),
        )

    def _from_delta(self, previous: BookTop, payload: dict[str, Any]) -> BookTop:
        # Kalshi deltas identify side, price, and signed size change. Top-only
        # state can safely apply updates at the current touch; deeper changes
        # require a fresh snapshot before they can become the new touch.
        side = payload.get("side")
        price = int(payload["price"])
        delta = int(payload.get("delta", payload.get("size_delta", 0)))
        bid, ask = previous.yes_bid_cents, previous.yes_ask_cents
        bid_size, ask_size = previous.yes_bid_size, previous.yes_ask_size

        if side == "yes":
            if bid is None or price > bid:
                if delta <= 0:
                    raise SequenceGapError("cannot remove unknown deeper YES level")
                bid, bid_size = price, delta
            elif price == bid:
                bid_size += delta
                if bid_size <= 0:
                    raise SequenceGapError("best YES level depleted; snapshot required")
        elif side == "no":
            candidate_ask = 100 - price
            if ask is None or candidate_ask < ask:
                if delta <= 0:
                    raise SequenceGapError("cannot remove unknown deeper NO level")
                ask, ask_size = candidate_ask, delta
            elif candidate_ask == ask:
                ask_size += delta
                if ask_size <= 0:
                    raise SequenceGapError("best NO level depleted; snapshot required")
        else:
            raise ValueError(f"unsupported order-book side: {side}")

        return BookTop(
            ticker=previous.ticker,
            yes_bid_cents=bid,
            yes_ask_cents=ask,
            yes_bid_size=bid_size,
            yes_ask_size=ask_size,
            captured_at=datetime.now(timezone.utc),
        )

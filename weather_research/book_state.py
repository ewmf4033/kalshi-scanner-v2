from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .models import BookTop
from .ws_protocol import SequenceGapError, SequenceTracker


def _cents(value: Any) -> int:
    """Convert fixed-point dollar strings to integer cents, half-up."""
    if isinstance(value, str) and "." in value:
        return int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int(value)


def _size(value: Any) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class OrderBookState:
    """Sequence-safe top-of-book state from unified-YES Kalshi messages.

    With ``use_yes_price: true``, NO-side prices are already expressed on the
    YES scale. They are offers, so the lowest NO-side level is the best YES ask.
    Any uncertain transition clears local state and requires a fresh snapshot.
    """

    tracker: SequenceTracker = field(default_factory=SequenceTracker)
    books: dict[str, BookTop] = field(default_factory=dict)

    def apply(self, message: dict[str, Any]) -> BookTop | None:
        msg_type = message.get("type")
        if msg_type not in {"orderbook_snapshot", "snapshot", "orderbook_delta", "delta"}:
            return None
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
            else:
                self.tracker.accept_delta(sid, seq)
                previous = self.books.get(ticker)
                if previous is None:
                    raise SequenceGapError(f"delta received without ticker snapshot: {ticker}")
                book = self._from_delta(previous, payload)
        except SequenceGapError:
            self.books.clear()
            raise

        self.books[ticker] = book
        return book

    @staticmethod
    def _levels(payload: dict[str, Any], side: str) -> list[list[Any]]:
        return (
            payload.get(f"{side}_dollars_fp")
            or payload.get(side)
            or payload.get(f"{side}_bids")
            or []
        )

    @staticmethod
    def _best(levels: list[list[Any]], *, highest: bool) -> tuple[int | None, int]:
        if not levels:
            return None, 0
        parsed = [(_cents(price), _size(size)) for price, size in levels]
        return max(parsed) if highest else min(parsed)

    def _from_snapshot(self, ticker: str, payload: dict[str, Any]) -> BookTop:
        yes_bid, yes_bid_size = self._best(self._levels(payload, "yes"), highest=True)
        yes_ask, yes_ask_size = self._best(self._levels(payload, "no"), highest=False)
        return BookTop(
            ticker=ticker,
            yes_bid_cents=yes_bid,
            yes_ask_cents=yes_ask,
            yes_bid_size=yes_bid_size,
            yes_ask_size=yes_ask_size,
            captured_at=datetime.now(timezone.utc),
        )

    def _from_delta(self, previous: BookTop, payload: dict[str, Any]) -> BookTop:
        side = payload.get("side")
        price = _cents(payload.get("price_dollars", payload.get("price")))
        delta = _size(payload.get("delta_fp", payload.get("delta", payload.get("size_delta", 0))))
        # Preserve sign after Decimal conversion.
        if Decimal(str(payload.get("delta_fp", payload.get("delta", payload.get("size_delta", 0))))) < 0:
            delta = -abs(delta)

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
            if ask is None or price < ask:
                if delta <= 0:
                    raise SequenceGapError("cannot remove unknown deeper NO level")
                ask, ask_size = price, delta
            elif price == ask:
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

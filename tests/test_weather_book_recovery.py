import pytest

from weather_research.book_state import OrderBookState
from weather_research.ws_protocol import SequenceGapError


def snapshot(ticker: str, seq: int) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 7,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes": [[40, 10]],
            "no": [[60, 12]],
        },
    }


def delta(ticker: str, seq: int, *, price: int = 41, change: int = 5) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 7,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": "yes",
            "price": price,
            "delta": change,
        },
    }


def test_missing_ticker_snapshot_does_not_erase_other_books():
    state = OrderBookState()
    state.apply(snapshot("A", 100))
    state.apply(snapshot("B", 101))

    with pytest.raises(SequenceGapError, match="without ticker snapshot"):
        state.apply(delta("C", 102))

    assert set(state.books) == {"A", "B"}


def test_ticker_local_reconstruction_failure_only_evicts_that_ticker():
    state = OrderBookState()
    state.apply(snapshot("A", 100))
    state.apply(snapshot("B", 101))

    with pytest.raises(SequenceGapError, match="depleted"):
        state.apply(delta("B", 102, price=40, change=-10))

    assert set(state.books) == {"A"}


def test_true_subscription_sequence_gap_still_clears_all_books():
    state = OrderBookState()
    state.apply(snapshot("A", 100))
    state.apply(snapshot("B", 101))

    with pytest.raises(SequenceGapError, match="expected 102, got 103"):
        state.apply(delta("A", 103))

    assert state.books == {}

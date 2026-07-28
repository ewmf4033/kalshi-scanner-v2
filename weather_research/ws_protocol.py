from __future__ import annotations


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

from __future__ import annotations

from collections.abc import Iterable

from .models import BookTop, BucketContract, Signal, ThresholdContract


def realized_threshold_signal(contract: ThresholdContract, book: BookTop, observed_value: float) -> Signal | None:
    """Return a single-leg certainty signal when the observed value fixes payoff."""
    yes_certain = (
        (contract.comparator == ">=" and observed_value >= contract.threshold)
        or (contract.comparator == ">" and observed_value > contract.threshold)
        or (contract.comparator == "<=" and observed_value <= contract.threshold)
        or (contract.comparator == "<" and observed_value < contract.threshold)
    )
    if not yes_certain or book.yes_ask_cents is None:
        return None
    return Signal(
        ticker=contract.ticker,
        kind="realized_threshold_yes",
        side="yes",
        executable_price_cents=book.yes_ask_cents,
        gross_gap_cents=100 - book.yes_ask_cents,
        displayed_size=book.yes_ask_size,
        observed_value=observed_value,
        reason=f"Observed {observed_value} makes {contract.comparator} {contract.threshold} true",
    )


def eliminated_bucket_signal(contract: BucketContract, book: BookTop, running_high: float) -> Signal | None:
    """A daily-high bucket is impossible once running high exceeds its upper edge."""
    if contract.upper is None:
        return None
    eliminated = running_high > contract.upper or (
        running_high == contract.upper and not contract.upper_inclusive
    )
    if not eliminated or book.yes_bid_cents is None:
        return None
    no_ask = 100 - book.yes_bid_cents
    return Signal(
        ticker=contract.ticker,
        kind="realized_bucket_elimination",
        side="no",
        executable_price_cents=no_ask,
        gross_gap_cents=100 - no_ask,
        displayed_size=book.yes_bid_size,
        observed_value=running_high,
        reason=f"Running high {running_high} is above bucket upper edge {contract.upper}",
    )


def monotonicity_violations(contracts: Iterable[ThresholdContract], books: dict[str, BookTop]) -> list[dict[str, int | str]]:
    """Find executable two-leg violations using unified YES-price books only."""
    ordered = sorted(contracts, key=lambda c: c.threshold)
    out: list[dict[str, int | str]] = []
    for low, high in zip(ordered, ordered[1:]):
        low_book = books.get(low.ticker)
        high_book = books.get(high.ticker)
        if not low_book or not high_book:
            continue
        if low_book.yes_ask_cents is None or high_book.yes_bid_cents is None:
            continue
        gross_lock_cents = high_book.yes_bid_cents - low_book.yes_ask_cents
        if gross_lock_cents > 0:
            out.append({
                "lower_ticker": low.ticker,
                "higher_ticker": high.ticker,
                "lower_yes_ask_cents": low_book.yes_ask_cents,
                "higher_yes_bid_cents": high_book.yes_bid_cents,
                "gross_lock_cents": gross_lock_cents,
                "max_size": min(low_book.yes_ask_size, high_book.yes_bid_size),
            })
    return out

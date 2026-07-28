from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from .ev import taker_fee_cents
from .models import BookTop, BucketContract, Signal, ThresholdContract

ObservationType = Literal["daily_high", "daily_low"]


def realized_threshold_signal(
    contract: ThresholdContract,
    book: BookTop,
    observed_value: float,
    observation_type: ObservationType,
) -> Signal | None:
    """Return a YES certainty signal only for intraday-monotone observations.

    A running daily high can only lock >=/> contracts. A running daily low can
    only lock <=/< contracts. Opposite comparators remain uncertain until the
    climatological day closes.
    """
    if observation_type == "daily_high":
        yes_certain = (
            (contract.comparator == ">=" and observed_value >= contract.threshold)
            or (contract.comparator == ">" and observed_value > contract.threshold)
        )
    elif observation_type == "daily_low":
        yes_certain = (
            (contract.comparator == "<=" and observed_value <= contract.threshold)
            or (contract.comparator == "<" and observed_value < contract.threshold)
        )
    else:
        raise ValueError(f"unsupported observation_type: {observation_type}")

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
        reason=(
            f"Running {observation_type} {observed_value} permanently satisfies "
            f"{contract.comparator} {contract.threshold}"
        ),
    )


def eliminated_bucket_signal(
    contract: BucketContract,
    book: BookTop,
    observed_value: float,
    observation_type: ObservationType,
) -> Signal | None:
    """Return a NO certainty signal when a monotone observation exits a bucket."""
    if observation_type == "daily_high":
        if contract.upper is None:
            return None
        eliminated = observed_value > contract.upper or (
            observed_value == contract.upper and not contract.upper_inclusive
        )
        edge = contract.upper
        direction = "above"
    elif observation_type == "daily_low":
        if contract.lower is None:
            return None
        eliminated = observed_value < contract.lower or (
            observed_value == contract.lower and not contract.lower_inclusive
        )
        edge = contract.lower
        direction = "below"
    else:
        raise ValueError(f"unsupported observation_type: {observation_type}")

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
        observed_value=observed_value,
        reason=f"Running {observation_type} {observed_value} is {direction} bucket edge {edge}",
    )


def monotonicity_violations(
    contracts: Iterable[ThresholdContract],
    books: dict[str, BookTop],
    *,
    contracts_per_leg: int = 100,
    margin_cents: float = 0.0,
) -> list[dict[str, int | float | str]]:
    """Find fee-positive executable two-leg locks on a uniform comparator ladder."""
    ordered = sorted(contracts, key=lambda c: c.threshold)
    if len(ordered) < 2:
        return []
    comparators = {c.comparator for c in ordered}
    if len(comparators) != 1:
        raise ValueError("all threshold comparators must match")
    comparator = ordered[0].comparator
    if comparator not in {">=", ">", "<=", "<"}:
        raise ValueError(f"unsupported comparator: {comparator}")

    out: list[dict[str, int | float | str]] = []
    for low, high in zip(ordered, ordered[1:]):
        low_book = books.get(low.ticker)
        high_book = books.get(high.ticker)
        if not low_book or not high_book:
            continue

        if comparator in {">=", ">"}:
            # P(high threshold) <= P(low threshold).
            if low_book.yes_ask_cents is None or high_book.yes_bid_cents is None:
                continue
            leg_one_price = low_book.yes_ask_cents
            leg_two_price = 100 - high_book.yes_bid_cents  # buy NO on high threshold
            gross_lock = high_book.yes_bid_cents - low_book.yes_ask_cents
            max_size = min(low_book.yes_ask_size, high_book.yes_bid_size)
            structure = "buy_lower_yes_buy_higher_no"
        else:
            # P(low threshold) <= P(high threshold) for <=/< ladders.
            if high_book.yes_ask_cents is None or low_book.yes_bid_cents is None:
                continue
            leg_one_price = high_book.yes_ask_cents
            leg_two_price = 100 - low_book.yes_bid_cents  # buy NO on low threshold
            gross_lock = low_book.yes_bid_cents - high_book.yes_ask_cents
            max_size = min(high_book.yes_ask_size, low_book.yes_bid_size)
            structure = "buy_higher_yes_buy_lower_no"

        executable_size = min(contracts_per_leg, max_size)
        if executable_size <= 0:
            continue
        pair_fee = taker_fee_cents(leg_one_price, executable_size) + taker_fee_cents(
            leg_two_price, executable_size
        )
        net_lock = gross_lock - pair_fee
        if net_lock > margin_cents:
            out.append(
                {
                    "lower_ticker": low.ticker,
                    "higher_ticker": high.ticker,
                    "comparator": comparator,
                    "structure": structure,
                    "gross_lock_cents": gross_lock,
                    "pair_fee_cents": pair_fee,
                    "net_lock_cents": net_lock,
                    "max_size": max_size,
                    "fee_size": executable_size,
                }
            )
    return out

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .models import BucketContract, ThresholdContract, WeatherRule
from .runner import MarketDefinition


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryResult:
    definition: MarketDefinition
    accepted_tickers: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DiscoveryError(f"invalid strike value {value!r}") from exc


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise DiscoveryError("missing close_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryError(f"invalid close_time {value!r}") from exc
    if parsed.tzinfo is None:
        raise DiscoveryError("close_time must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _required_bool(market: dict[str, Any], field: str) -> bool:
    value = market.get(field)
    if not isinstance(value, bool):
        raise DiscoveryError(f"strike_type=between requires explicit boolean {field}")
    return value


def _display_confirms_closed_integer_range(market: dict[str, Any], floor: float, cap: float) -> bool:
    """Validate Kalshi's documented display range when inclusivity fields are absent."""
    if not floor.is_integer() or not cap.is_integer():
        return False
    lo = int(floor)
    hi = int(cap)
    subtitle = str(market.get("subtitle") or "").strip()
    title = str(market.get("title") or "")
    subtitle_match = re.fullmatch(rf"{lo}\s*°?\s+to\s+{hi}\s*°?", subtitle, flags=re.IGNORECASE)
    title_match = re.search(rf"(?<!\d){lo}\s*[-–]\s*{hi}\s*°", title)
    return subtitle_match is not None or title_match is not None


def _between_inclusivity(market: dict[str, Any], floor: float, cap: float) -> tuple[bool, bool]:
    lower = market.get("lower_inclusive")
    upper = market.get("upper_inclusive")
    if isinstance(lower, bool) and isinstance(upper, bool):
        return lower, upper
    if lower is None and upper is None and _display_confirms_closed_integer_range(market, floor, cap):
        return True, True
    if lower is None and upper is None:
        raise DiscoveryError("between inclusivity absent and display range is not an exact closed integer range")
    raise DiscoveryError("between inclusivity fields must both be booleans or both be absent")


def market_to_contract(market: dict[str, Any]):
    """Map explicit strike semantics and authenticated Kalshi display metadata."""
    ticker = str(market.get("ticker") or "")
    if not ticker:
        raise DiscoveryError("market is missing ticker")
    if market.get("is_provisional") is True:
        raise DiscoveryError("provisional market")

    strike_type = str(market.get("strike_type") or "").strip().lower()
    floor = _number(market.get("floor_strike"))
    cap = _number(market.get("cap_strike"))

    if strike_type == "greater":
        if floor is None or cap is not None:
            raise DiscoveryError("greater requires floor_strike only")
        return ThresholdContract(ticker=ticker, comparator=">", threshold=floor)
    if strike_type == "greater_or_equal":
        if floor is None or cap is not None:
            raise DiscoveryError("greater_or_equal requires floor_strike only")
        return ThresholdContract(ticker=ticker, comparator=">=", threshold=floor)
    if strike_type == "less":
        if cap is None or floor is not None:
            raise DiscoveryError("less requires cap_strike only")
        return ThresholdContract(ticker=ticker, comparator="<", threshold=cap)
    if strike_type == "less_or_equal":
        if cap is None or floor is not None:
            raise DiscoveryError("less_or_equal requires cap_strike only")
        return ThresholdContract(ticker=ticker, comparator="<=", threshold=cap)
    if strike_type == "between":
        if floor is None or cap is None:
            raise DiscoveryError("between requires floor_strike and cap_strike")
        if floor > cap:
            raise DiscoveryError("floor strike exceeds cap strike")
        lower_inclusive, upper_inclusive = _between_inclusivity(market, floor, cap)
        return BucketContract(
            ticker=ticker,
            lower=floor,
            upper=cap,
            lower_inclusive=lower_inclusive,
            upper_inclusive=upper_inclusive,
        )
    raise DiscoveryError(f"unsupported or missing strike_type {strike_type!r}")


def validate_bucket_partition(buckets: Iterable[BucketContract]) -> None:
    """Fail closed when adjacent integer-settlement buckets overlap or leave a gap."""
    ordered = sorted(
        buckets,
        key=lambda row: (
            float("-inf") if row.lower is None else row.lower,
            float("inf") if row.upper is None else row.upper,
            row.ticker,
        ),
    )
    for bucket in ordered:
        if bucket.lower is None or bucket.upper is None:
            raise DiscoveryError(f"bucket {bucket.ticker} must have finite bounds")
        if not float(bucket.lower).is_integer() or not float(bucket.upper).is_integer():
            raise DiscoveryError(f"bucket {bucket.ticker} has non-integer weather bounds")
    for left, right in zip(ordered, ordered[1:]):
        assert left.upper is not None and right.lower is not None
        delta = right.lower - left.upper
        if delta < 0:
            raise DiscoveryError(f"bucket overlap: {left.ticker} and {right.ticker}")
        if delta == 0:
            if left.upper_inclusive == right.lower_inclusive:
                kind = "overlap" if left.upper_inclusive else "gap"
                raise DiscoveryError(f"bucket boundary {kind} at {left.upper}: {left.ticker}/{right.ticker}")
        elif delta == 1:
            if not (left.upper_inclusive and right.lower_inclusive):
                raise DiscoveryError(f"integer bucket gap between {left.ticker} and {right.ticker}")
        else:
            raise DiscoveryError(f"bucket gap between {left.ticker} and {right.ticker}")


def discover_definition(
    rule: WeatherRule,
    markets: Iterable[dict[str, Any]],
    *,
    require_nonempty: bool = True,
    now: datetime | None = None,
    horizon_hours: float | None = None,
) -> DiscoveryResult:
    thresholds: list[ThresholdContract] = []
    buckets: list[BucketContract] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    if horizon_hours is not None and horizon_hours <= 0:
        raise ValueError("horizon_hours must be positive")
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    horizon_end = now_utc + timedelta(hours=horizon_hours) if horizon_hours is not None else None

    for market in markets:
        ticker = str(market.get("ticker") or "<missing>")
        status = str(market.get("status") or "").strip().lower()
        if status not in {"open", "unopened", "active"}:
            rejected.append((ticker, f"unsupported status {status!r}"))
            continue
        if horizon_end is not None:
            try:
                close_time = _timestamp(market.get("close_time"))
            except DiscoveryError as exc:
                rejected.append((ticker, str(exc)))
                continue
            if close_time < now_utc:
                rejected.append((ticker, "close_time is in the past"))
                continue
            if close_time > horizon_end:
                rejected.append((ticker, "outside discovery horizon"))
                continue
        try:
            contract = market_to_contract(market)
        except DiscoveryError as exc:
            rejected.append((ticker, str(exc)))
            continue
        if contract.ticker in seen:
            rejected.append((contract.ticker, "duplicate ticker"))
            continue
        seen.add(contract.ticker)
        if isinstance(contract, ThresholdContract):
            thresholds.append(contract)
        else:
            buckets.append(contract)

    thresholds.sort(key=lambda row: (row.threshold, row.ticker))
    buckets.sort(key=lambda row: (
        float("-inf") if row.lower is None else row.lower,
        float("inf") if row.upper is None else row.upper,
        row.ticker,
    ))
    if len(buckets) > 1:
        validate_bucket_partition(buckets)
    definition = MarketDefinition(rule=rule, thresholds=tuple(thresholds), buckets=tuple(buckets))
    accepted = tuple(sorted(seen))
    if require_nonempty and not accepted:
        detail = "; ".join(f"{ticker}: {reason}" for ticker, reason in rejected[:10])
        raise DiscoveryError(f"no usable markets discovered for {rule.series_ticker}: {detail}")
    return DiscoveryResult(definition, accepted, tuple(rejected))


def discover_all(kalshi, definitions: dict[str, MarketDefinition]) -> dict[str, DiscoveryResult]:
    results: dict[str, DiscoveryResult] = {}
    for series_ticker, existing in definitions.items():
        markets = kalshi.list_markets(series_ticker=series_ticker, statuses=("open", "unopened"))
        results[series_ticker] = discover_definition(existing.rule, markets)
    return results

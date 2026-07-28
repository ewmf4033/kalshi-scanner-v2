from __future__ import annotations

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


def market_to_contract(market: dict[str, Any]):
    """Map only unambiguous structured strikes; never parse titles as authority."""
    ticker = str(market.get("ticker") or "")
    if not ticker:
        raise DiscoveryError("market is missing ticker")
    if market.get("is_provisional") is True:
        raise DiscoveryError("provisional market")

    floor = _number(market.get("floor_strike"))
    cap = _number(market.get("cap_strike"))
    if floor is None and cap is None:
        raise DiscoveryError("missing structured floor/cap strike")
    if floor is not None and cap is not None:
        if floor > cap:
            raise DiscoveryError("floor strike exceeds cap strike")
        return BucketContract(
            ticker=ticker,
            lower=floor,
            upper=cap,
            lower_inclusive=True,
            upper_inclusive=True,
        )
    if floor is not None:
        return ThresholdContract(ticker=ticker, comparator=">=", threshold=floor)
    return ThresholdContract(ticker=ticker, comparator="<=", threshold=cap)


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
        status = str(market.get("status") or "")
        if status not in {"open", "unopened"}:
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
    definition = MarketDefinition(rule=rule, thresholds=tuple(thresholds), buckets=tuple(buckets))
    accepted = tuple(sorted(seen))
    if require_nonempty and not accepted:
        detail = "; ".join(f"{ticker}: {reason}" for ticker, reason in rejected[:10])
        raise DiscoveryError(f"no usable markets discovered for {rule.series_ticker}: {detail}")
    return DiscoveryResult(definition, accepted, tuple(rejected))


def discover_all(kalshi, definitions: dict[str, MarketDefinition]) -> dict[str, DiscoveryResult]:
    results: dict[str, DiscoveryResult] = {}
    for series_ticker, existing in definitions.items():
        markets = kalshi.list_markets(series_ticker=series_ticker)
        results[series_ticker] = discover_definition(existing.rule, markets)
    return results

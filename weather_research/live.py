from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

from .book_state import OrderBookState
from .discovery import discover_definition
from .kalshi_api import KalshiClient
from .models import BucketContract, ThresholdContract, WeatherRule
from .observations import NWSObservationClient, local_day_window
from .runner import MarketDefinition, WeatherResearchRunner
from .storage import ResearchStore
from .ws_protocol import SequenceGapError, orderbook_subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveConfig:
    definitions: dict[str, MarketDefinition]
    poll_seconds: float = 60.0
    database_path: str = "weather_research.sqlite3"
    auto_discover: bool = True
    discovery_seconds: float = 900.0

    @property
    def market_tickers(self) -> list[str]:
        tickers: list[str] = []
        for definition in self.definitions.values():
            tickers.extend(c.ticker for c in definition.thresholds)
            tickers.extend(c.ticker for c in definition.buckets)
        return sorted(set(tickers))


def load_config(path: str | Path) -> LiveConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    definitions: dict[str, MarketDefinition] = {}
    for item in data["markets"]:
        rule = WeatherRule(**item["rule"])
        definitions[rule.series_ticker] = MarketDefinition(
            rule=rule,
            thresholds=tuple(ThresholdContract(**row) for row in item.get("thresholds", [])),
            buckets=tuple(BucketContract(**row) for row in item.get("buckets", [])),
        )
    discovery_seconds = float(data.get("discovery_seconds", 900))
    if discovery_seconds < 30:
        raise ValueError("discovery_seconds must be at least 30")
    return LiveConfig(
        definitions=definitions,
        poll_seconds=float(data.get("poll_seconds", 60)),
        database_path=str(data.get("database_path", "weather_research.sqlite3")),
        auto_discover=bool(data.get("auto_discover", True)),
        discovery_seconds=discovery_seconds,
    )


class LiveWeatherLogger:
    def __init__(self, config: LiveConfig, kalshi: KalshiClient | None = None) -> None:
        self.config = config
        self.kalshi = kalshi or KalshiClient.from_env()
        self.store = ResearchStore(config.database_path)
        self.runner = WeatherResearchRunner(config.definitions, self.store)
        self.nws = NWSObservationClient()
        self._stop = asyncio.Event()
        self._command_id = 1

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await asyncio.gather(self._book_loop(), self._observation_loop())

    async def _refresh_catalog(self) -> bool:
        """Replace recurring contracts atomically; API failures leave the last catalog intact."""
        if not self.config.auto_discover:
            return False
        new_definitions: dict[str, MarketDefinition] = {}
        rejected_total = 0
        for series_ticker, existing in self.config.definitions.items():
            markets = await asyncio.to_thread(
                self.kalshi.list_markets, series_ticker=series_ticker
            )
            result = discover_definition(existing.rule, markets, require_nonempty=False)
            new_definitions[series_ticker] = result.definition
            rejected_total += len(result.rejected)
            if result.rejected:
                log.warning(
                    "ticker discovery rejected %d markets for %s: %s",
                    len(result.rejected),
                    series_ticker,
                    result.rejected[:5],
                )

        old_tickers = tuple(self.config.market_tickers)
        new_tickers = tuple(sorted({
            contract.ticker
            for definition in new_definitions.values()
            for contract in (*definition.thresholds, *definition.buckets)
        }))
        self.config.definitions.clear()
        self.config.definitions.update(new_definitions)
        self.runner.definitions = self.config.definitions
        changed = old_tickers != new_tickers
        if changed:
            # Never carry a book or quote-age clock across a contract roll.
            self.runner.books = OrderBookState()
            self.runner.quote_first_seen.clear()
            log.info(
                "weather catalog changed: %d -> %d tickers (%d rejected)",
                len(old_tickers), len(new_tickers), rejected_total,
            )
        return changed

    async def _book_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._refresh_catalog()
                tickers = self.config.market_tickers
                if not tickers:
                    log.warning("no open or unopened weather markets discovered; retrying")
                    await asyncio.sleep(min(self.config.discovery_seconds, 60))
                    continue

                headers = self.kalshi.websocket_headers()
                async with websockets.connect(
                    self.kalshi.websocket_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=10_000,
                ) as ws:
                    await ws.send(json.dumps(orderbook_subscription(self._next_id(), tickers)))
                    refresh_at = asyncio.get_running_loop().time() + self.config.discovery_seconds
                    while not self._stop.is_set():
                        timeout = max(0.05, min(1.0, refresh_at - asyncio.get_running_loop().time()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            if asyncio.get_running_loop().time() >= refresh_at:
                                changed = await self._refresh_catalog()
                                refresh_at = asyncio.get_running_loop().time() + self.config.discovery_seconds
                                if changed:
                                    break  # reconnect with the new exact ticker set
                            continue
                        message: dict[str, Any] = json.loads(raw)
                        try:
                            self.runner.ingest_book_message(message)
                        except SequenceGapError as exc:
                            log.warning("sequence invalidation: %s", exc)
                            await ws.send(json.dumps(self._snapshot_request(message)))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WebSocket/catalog loop failed; reconnecting")
                await asyncio.sleep(2)

    async def _observation_loop(self) -> None:
        while not self._stop.is_set():
            receipt_time = datetime.now(timezone.utc)
            # Copy because catalog refresh mutates the dictionary atomically between awaits.
            for series_ticker, definition in list(self.config.definitions.items()):
                rule = definition.rule
                try:
                    start, end = local_day_window(
                        receipt_time,
                        rule.timezone,
                        time_basis=rule.time_basis,
                        standard_utc_offset_minutes=rule.standard_utc_offset_minutes,
                    )
                    rows = await asyncio.to_thread(self.nws.range, rule.station_id, start, end)
                    self.runner.ingest_day_observations(
                        series_ticker, rows, receipt_time=receipt_time
                    )
                except Exception:
                    log.exception(
                        "full-day observation poll failed for %s/%s",
                        series_ticker,
                        rule.station_id,
                    )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def _next_id(self) -> int:
        value = self._command_id
        self._command_id += 1
        return value

    def _snapshot_request(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("msg", message.get("data", {}))
        ticker = payload.get("market_ticker") or payload.get("ticker")
        if not ticker:
            raise ValueError("cannot request snapshot without ticker")
        return {
            "id": self._next_id(),
            "cmd": "update_subscription",
            "params": {
                "sid": int(message.get("sid", message.get("subscription_id", 0))),
                "action": "get_snapshot",
                "market_tickers": [ticker],
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Kalshi structural weather logger")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = LiveWeatherLogger(load_config(args.config))
    try:
        asyncio.run(logger.run())
    finally:
        logger.store.close()


if __name__ == "__main__":
    main()

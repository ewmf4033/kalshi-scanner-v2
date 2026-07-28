from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .book_state import OrderBookState
from .ev import minimum_gap_cents
from .models import BucketContract, Signal, ThresholdContract, WeatherRule
from .observations import update_running_extreme
from .reconcile import ReconciliationStats
from .signals import eliminated_bucket_signal, realized_threshold_signal
from .storage import ResearchStore


@dataclass(frozen=True)
class MarketDefinition:
    rule: WeatherRule
    thresholds: tuple[ThresholdContract, ...] = ()
    buckets: tuple[BucketContract, ...] = ()


@dataclass
class WeatherResearchRunner:
    definitions: dict[str, MarketDefinition]
    store: ResearchStore
    contract_count: int = 100
    min_depth: int = 50
    min_survival_seconds: float = 3.0
    safety_margin_cents: float = 1.0
    slippage_cents: float = 0.0
    books: OrderBookState = field(default_factory=OrderBookState)
    running_extremes: dict[str, float] = field(default_factory=dict)
    quote_first_seen: dict[tuple[str, str, int], datetime] = field(default_factory=dict)

    def current_error_bound(self) -> float:
        """Use the powered all-station-day cohort for the entry threshold."""
        total, errors = self.store.reconciliation_counts()
        return ReconciliationStats(total=total, errors=errors).wilson_upper()

    def required_gap(self, price_cents: int, executable_size: int) -> float:
        contracts = max(1, min(self.contract_count, executable_size))
        return minimum_gap_cents(
            self.current_error_bound(), price_cents, contracts,
            slippage_cents=self.slippage_cents,
            safety_margin_cents=self.safety_margin_cents,
        )

    def reconciliation_bounds(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, kwargs in (
            ("baseline", {}), ("signal", {"signal_only": True}), ("fill", {"fill_only": True})
        ):
            total, errors = self.store.reconciliation_counts(**kwargs)
            out[name] = ReconciliationStats(total, errors).wilson_upper()
        return out

    def reconcile_day(
        self, *, station_id: str, date: str, parsed_value: float, settled_value: float,
        signal_fired: bool, would_have_filled: bool, tolerance_tenths: int = 0,
    ) -> None:
        self.store.add_reconciliation(
            station_id=station_id, date=date, parsed_value=parsed_value,
            settled_value=settled_value, signal_fired=signal_fired,
            would_have_filled=would_have_filled, tolerance_tenths=tolerance_tenths,
        )

    def ingest_book_message(self, message: dict) -> list[Signal]:
        now = datetime.now(timezone.utc)
        self.store.add_book_event(now.isoformat(), str(message.get("type")), message)
        book = self.books.apply(message)
        if book is None:
            return []
        return self._evaluate_ticker(book.ticker, now)

    def ingest_observation(self, station_id: str, temperature_f: float, observed_at: datetime) -> list[Signal]:
        emitted: list[Signal] = []
        for definition in self.definitions.values():
            rule = definition.rule
            if rule.station_id != station_id:
                continue
            running = update_running_extreme(
                self.running_extremes.get(rule.series_ticker), temperature_f, rule.observation_type
            )
            self.running_extremes[rule.series_ticker] = running
            self.store.add_observation(
                station_id, observed_at.isoformat(), temperature_f, running, rule.observation_type
            )
            tickers = [c.ticker for c in definition.thresholds] + [c.ticker for c in definition.buckets]
            for ticker in tickers:
                emitted.extend(self._evaluate_ticker(ticker, observed_at))
        return emitted

    def _evaluate_ticker(self, ticker: str, now: datetime) -> list[Signal]:
        book = self.books.books.get(ticker)
        if book is None:
            self._clear_quote_age(ticker)
            return []
        out: list[Signal] = []
        for definition in self.definitions.values():
            running = self.running_extremes.get(definition.rule.series_ticker)
            if running is None:
                continue
            for contract in definition.thresholds:
                if contract.ticker == ticker:
                    signal = realized_threshold_signal(contract, book, running, definition.rule.observation_type)
                    if signal:
                        out.append(signal)
            for contract in definition.buckets:
                if contract.ticker == ticker:
                    signal = eliminated_bucket_signal(contract, book, running, definition.rule.observation_type)
                    if signal:
                        out.append(signal)

        active_keys = {(s.ticker, s.side, s.executable_price_cents) for s in out}
        for key in [k for k in self.quote_first_seen if k[0] == ticker and k not in active_keys]:
            del self.quote_first_seen[key]
        for signal in out:
            self._persist_signal(signal, now)
        return out

    def _clear_quote_age(self, ticker: str) -> None:
        for key in [k for k in self.quote_first_seen if k[0] == ticker]:
            del self.quote_first_seen[key]

    def _persist_signal(self, signal: Signal, now: datetime) -> None:
        key = (signal.ticker, signal.side, signal.executable_price_cents)
        first_seen = self.quote_first_seen.setdefault(key, now)
        age = max(0.0, (now - first_seen).total_seconds())
        threshold = self.required_gap(signal.executable_price_cents, signal.displayed_size)
        would_fill = (
            signal.displayed_size >= self.min_depth
            and age >= self.min_survival_seconds
            and signal.gross_gap_cents >= threshold
        )
        self.store.add_signal(now.isoformat(), signal, threshold, age, would_fill)

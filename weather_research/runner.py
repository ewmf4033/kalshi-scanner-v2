from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .book_state import OrderBookState
from .ev import minimum_gap_cents
from .models import BucketContract, Signal, ThresholdContract, WeatherRule
from .observations import (
    StationObservation,
    apply_rule_rounding,
    climatological_date,
    recompute_candidate_extremes,
    recompute_day_extreme,
    rounding_candidates,
    update_running_extreme,
)
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
    running_extremes: dict[tuple[str, date], float] = field(default_factory=dict)
    candidate_extremes: dict[tuple[str, date], tuple[float, float, float]] = field(default_factory=dict)
    current_dates: dict[str, date] = field(default_factory=dict)
    quote_first_seen: dict[tuple[str, str, int], datetime] = field(default_factory=dict)

    def current_error_bound(self) -> float:
        total, errors = self.store.reconciliation_counts(candidate="selected")
        return ReconciliationStats(total=total, errors=errors).wilson_upper()

    def required_gap(self, price_cents: int, executable_size: int) -> float:
        contracts = max(1, min(self.contract_count, executable_size))
        return minimum_gap_cents(
            self.current_error_bound(), price_cents, contracts,
            slippage_cents=self.slippage_cents,
            safety_margin_cents=self.safety_margin_cents,
        )

    def reconciliation_bounds(self, *, candidate: str = "selected") -> dict[str, float]:
        out: dict[str, float] = {}
        for name, kwargs in (
            ("baseline", {}), ("signal", {"signal_only": True}), ("fill", {"fill_only": True})
        ):
            total, errors = self.store.reconciliation_counts(candidate=candidate, **kwargs)
            out[name] = ReconciliationStats(total, errors).wilson_upper()
        return out

    def reconcile_day(
        self,
        *,
        station_id: str,
        date: str,
        parsed_cf_value: float,
        parsed_c_value: float,
        parsed_cff_value: float,
        selected_parsed_value: float,
        settled_value: float,
        signal_fired: bool,
        would_have_filled: bool,
        tolerance_tenths: int = 0,
    ) -> None:
        self.store.add_reconciliation(
            station_id=station_id,
            date=date,
            parsed_cf_value=parsed_cf_value,
            parsed_c_value=parsed_c_value,
            parsed_cff_value=parsed_cff_value,
            selected_parsed_value=selected_parsed_value,
            settled_value=settled_value,
            signal_fired=signal_fired,
            would_have_filled=would_have_filled,
            tolerance_tenths=tolerance_tenths,
        )

    def ingest_book_message(self, message: dict) -> list[Signal]:
        now = datetime.now(timezone.utc)
        self.store.add_book_event(now.isoformat(), str(message.get("type")), message)
        book = self.books.apply(message)
        if book is None:
            return []
        return self._evaluate_ticker(book.ticker, now)

    @staticmethod
    def _climate_date(observed_at: datetime, rule: WeatherRule) -> date:
        return climatological_date(
            observed_at,
            rule.timezone,
            time_basis=rule.time_basis,
            standard_utc_offset_minutes=rule.standard_utc_offset_minutes,
        )

    def ingest_observation(self, station_id: str, temperature_c: float, observed_at: datetime) -> list[Signal]:
        """Ingest raw Celsius; quote survival always uses wall-clock receipt time."""
        receipt_time = datetime.now(timezone.utc)
        emitted: list[Signal] = []
        candidates = rounding_candidates(temperature_c)
        raw_f = temperature_c * 9 / 5 + 32
        for definition in self.definitions.values():
            rule = definition.rule
            if rule.station_id != station_id:
                continue
            local_date = self._climate_date(observed_at, rule)
            selected = apply_rule_rounding(raw_f, rule.rounding)
            key = (rule.series_ticker, local_date)
            selected_running = update_running_extreme(
                self.running_extremes.get(key), selected, rule.observation_type
            )
            previous_cf, previous_c, previous_cff = self.candidate_extremes.get(key, (None, None, None))
            running_cf = update_running_extreme(
                previous_cf, candidates.temperature_f_round_cf, rule.observation_type
            )
            running_c = update_running_extreme(
                previous_c, candidates.temperature_f_round_c, rule.observation_type
            )
            running_cff = update_running_extreme(
                previous_cff, candidates.temperature_f_round_cff, rule.observation_type
            )
            self.running_extremes[key] = selected_running
            self.candidate_extremes[key] = (running_cf, running_c, running_cff)
            self.current_dates[rule.series_ticker] = local_date
            self.store.add_observation(
                station_id=station_id,
                observed_at=observed_at.isoformat(),
                temperature_c=temperature_c,
                temperature_f_round_cf=candidates.temperature_f_round_cf,
                temperature_f_round_c=candidates.temperature_f_round_c,
                temperature_f_round_cff=candidates.temperature_f_round_cff,
                running_extreme_cf=running_cf,
                running_extreme_c=running_c,
                running_extreme_cff=running_cff,
                selected_temperature_f=selected,
                selected_running_extreme_f=selected_running,
                observation_type=rule.observation_type,
            )
            emitted.extend(self._evaluate_definition(definition, receipt_time, local_date))
        return emitted

    def ingest_day_observations(
        self, series_ticker: str, observations: list[StationObservation], receipt_time: datetime | None = None
    ) -> list[Signal]:
        """Recompute selected and candidate extremes from the complete climate-day set."""
        definition = self.definitions[series_ticker]
        rule = definition.rule
        if not observations:
            return []
        receipt_time = receipt_time or datetime.now(timezone.utc)
        local_dates = {self._climate_date(row.observed_at, rule) for row in observations}
        if len(local_dates) != 1:
            raise ValueError("day observation batch crosses climatological dates")
        local_date = next(iter(local_dates))
        selected_running = recompute_day_extreme(observations, rule.observation_type, rule.rounding)
        running_cf, running_c, running_cff = recompute_candidate_extremes(observations, rule.observation_type)
        key = (series_ticker, local_date)
        self.running_extremes[key] = selected_running
        self.candidate_extremes[key] = (running_cf, running_c, running_cff)
        self.current_dates[series_ticker] = local_date
        latest = max(observations, key=lambda row: row.observed_at)
        candidates = rounding_candidates(latest.temperature_c)
        selected = apply_rule_rounding(latest.temperature_f, rule.rounding)
        self.store.add_observation(
            station_id=rule.station_id,
            observed_at=latest.observed_at.isoformat(),
            temperature_c=latest.temperature_c,
            temperature_f_round_cf=candidates.temperature_f_round_cf,
            temperature_f_round_c=candidates.temperature_f_round_c,
            temperature_f_round_cff=candidates.temperature_f_round_cff,
            running_extreme_cf=running_cf,
            running_extreme_c=running_c,
            running_extreme_cff=running_cff,
            selected_temperature_f=selected,
            selected_running_extreme_f=selected_running,
            observation_type=rule.observation_type,
        )
        return self._evaluate_definition(definition, receipt_time, local_date)

    def _evaluate_definition(
        self, definition: MarketDefinition, now: datetime, local_date: date
    ) -> list[Signal]:
        emitted: list[Signal] = []
        tickers = [c.ticker for c in definition.thresholds] + [c.ticker for c in definition.buckets]
        for ticker in tickers:
            emitted.extend(self._evaluate_ticker(ticker, now, local_date=local_date))
        return emitted

    def _evaluate_ticker(
        self, ticker: str, now: datetime, local_date: date | None = None
    ) -> list[Signal]:
        book = self.books.books.get(ticker)
        if book is None:
            self._clear_quote_age(ticker)
            return []
        out: list[Signal] = []
        for definition in self.definitions.values():
            series = definition.rule.series_ticker
            active_date = local_date or self.current_dates.get(series)
            if active_date is None:
                continue
            running = self.running_extremes.get((series, active_date))
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

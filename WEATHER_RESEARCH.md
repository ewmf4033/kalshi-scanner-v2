# Structural Weather Research MVP

This package is a read-only falsification experiment. It does not place orders.

## Live path now included

- Authenticated Kalshi WebSocket connection using the recommended external API host.
- `use_yes_price: true` subscription and current fixed-point snapshot/delta parsing.
- Sequence-safe book state: any gap or uncertain top-level depletion discards local state and requests a fresh snapshot.
- NWS full-climate-day observation polling, recomputing each daily extreme from all available observations on every poll.
- Running extremes keyed by `(series_ticker, climatological_date)` using each rule's configured time basis.
- Rule-driven weather rounding before threshold evaluation.
- SQLite persistence for raw book messages, observations, signals, and reconciliations.
- A runner that computes the minimum acceptable gap from the current all-station-day Wilson upper bound. There is no fixed entry threshold.
- Separate baseline, signal-conditioned, and would-have-filled reconciliation bounds.

## Start

1. Copy `weather_research.example.json` and replace every placeholder only after the settlement-rule audit.
2. Set `KALSHI_API_KEY_ID` and either `KALSHI_PRIVATE_KEY_PEM` or `KALSHI_PRIVATE_KEY_PATH`.
3. Install `requirements-weather.txt`.
4. Run:

```bash
python -m weather_research.live --config weather_research.json
```

The logger uses `wss://external-api-ws.kalshi.com/trade-api/ws/v2` and signs `/trade-api/ws/v2`. These may be overridden with `KALSHI_WS_URL` and `KALSHI_REST_URL` for demo testing.

## Climate clock guard

Do not assume that an IANA civil timezone is the settlement clock. NWS CLI products label observation times as LST. For CLINYC, configure:

```json
"timezone": "America/New_York",
"time_basis": "local_standard",
"standard_utc_offset_minutes": -300
```

This uses fixed EST all year, so summer observations between midnight EDT and 1:00 a.m. EDT remain in the prior LST climate date. A `local_standard` rule without an explicit offset is rejected rather than inferred from a DST-observing zone.

## Daily operating requirement

Enter the official rulebook-named settlement value through `runner.reconcile_day()` every day from day one. Key reconciliation to the climate date named inside the final report, not its publication timestamp or a same-day preliminary CLI. Do not defer this to a month-end backfill. With zero reconciliation rows, the Wilson upper bound is 100%, the required gap exceeds $1.00, and no signal can be classified as `would_have_filled`.

## Rulebook rounding guard

The `rounding` field is load-bearing. Set it only from the audited settlement rule for that exact series and source. CLINYC reports whole-degree Fahrenheit, so the NYC example uses `nearest_int`; other series must be audited independently. Values such as 89.96°F become 90°F and can trigger certainty at the boundary.

## Scope

1. Resolve the authenticated incentive-program endpoint and inspect active/upcoming weather programs.
2. Subscribe to weather order books with `use_yes_price: true`.
3. Emit realized cumulative-threshold YES signals.
4. Emit realized interval-bucket elimination NO signals as a first-class output.
5. Detect executable adjacent-strike monotonicity violations from unified YES prices, with fees calculated at actual executable depth.
6. Reconcile the parser against every station-day settlement, then report baseline, signal-conditioned, and would-have-filled error separately.
7. Set any future entry gap from the statistically supported error bound, not from a fixed price floor.

## Hard boundaries

- Six-hour gate: valid data must be collecting by hour six.
- Twelve-hour total build/debug budget.
- No LLM probability forecasts, PCA, cross-platform feeds, generalized settlement compiler, or live order router.

## EV and evidence rule

For a contract bought at price `p` cents and believed certain:

`EV cents = (100 - p) - 100 * fill_conditional_mapping_error - fee - slippage`

The powered baseline cohort sets the automated gap threshold. Signal-days and would-have-filled days remain separate bias diagnostics. The system never substitutes a tiny fill-only sample for the baseline and never permits a config constant to override the evidence-derived gap.

Weather values are normalized to the configured rule precision before signal evaluation, and settlement comparisons use integer tenths with decimal half-up normalization.

## Validation

Run:

```bash
pytest -q tests/test_weather_research.py tests/test_weather_live.py
```

The focused suite covers unified YES pricing, intraday-safe certainty direction, bucket elimination, comparator-aware monotonicity, depth-aware fee rounding, error-derived thresholds, civil and fixed-standard climatological-day resets, full-day observation recomputation, receipt-time quote aging, reconciliation cohorts, decimal half-up boundaries, persistence, and sequence-gap poisoning.
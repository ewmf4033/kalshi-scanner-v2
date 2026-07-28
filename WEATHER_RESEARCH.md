# Structural Weather Research MVP

This package is a read-only falsification experiment. It does not place orders.

## Live path now included

- Authenticated Kalshi WebSocket connection using the recommended external API host.
- `use_yes_price: true` subscription and current fixed-point snapshot/delta parsing.
- Automatic recurring-market discovery from `GET /markets?series_ticker=...` for open and unopened contracts.
- Contract-roll handling that clears old books and quote clocks, then reconnects with the new exact ticker set.
- Sequence-safe book state: any gap or uncertain top-level depletion discards local state and requests a fresh snapshot.
- NWS full-climate-day observation polling, recomputing each daily extreme from all available observations on every poll.
- Running extremes keyed by `(series_ticker, climatological_date)` using each rule's configured time basis.
- Rule-driven weather rounding before threshold evaluation.
- Lossless weather persistence: raw Celsius, three candidate Fahrenheit paths, three independently computed daily extremes, and three settlement comparisons.
- SQLite persistence for raw book messages, observations, signals, and reconciliations.
- A runner that computes the minimum acceptable gap from the current all-station-day Wilson upper bound. There is no fixed entry threshold.
- Separate baseline, signal-conditioned, and would-have-filled reconciliation bounds.

## Start

1. Copy `weather_research.example.json` and finish the settlement-rule audit fields.
2. Set `KALSHI_API_KEY_ID` and either `KALSHI_PRIVATE_KEY_PEM` or `KALSHI_PRIVATE_KEY_PATH`.
3. Install `requirements-weather.txt`.
4. Run:

```bash
python -m weather_research.live --config weather_research.json
```

The logger uses `wss://external-api-ws.kalshi.com/trade-api/ws/v2` and signs `/trade-api/ws/v2`. These may be overridden with `KALSHI_WS_URL` and `KALSHI_REST_URL` for demo testing.

## Automatic ticker discovery

Set:

```json
"auto_discover": true,
"discovery_seconds": 900
```

For every configured `series_ticker`, the logger retrieves open and unopened markets, follows pagination cursors, bounds the catalog to the configured near-term horizon, and rebuilds the active contract set.

Discovery uses `strike_type` as authority and never infers a comparator from which strike field happens to be populated:

- `greater` → `>` with `floor_strike`;
- `greater_or_equal` → `>=` with `floor_strike`;
- `less` → `<` with `cap_strike`;
- `less_or_equal` → `<=` with `cap_strike`;
- `between` → an interval only when both bounds and explicit `lower_inclusive` / `upper_inclusive` booleans are present.

Unknown strike types, inconsistent fields, missing inclusivity, invalid bounds, unsupported status, duplicate ticker, or `is_provisional=true` are rejected. Titles are diagnostics only and are never parsed as settlement authority.

Adjacent integer-settlement buckets are validated as a partition. Overlap, a missing integer, or double ownership of a shared boundary fails discovery rather than generating signals.

Before merge or first launch, run the authenticated audit dump and compare it with the live market labels:

```bash
python -m weather_research.catalog_dump --series KXHIGHNY
```

The dump prints `ticker`, `strike_type`, `floor_strike`, `cap_strike`, inclusivity fields, `status`, and `close_time` for the full open/unopened ladder.

If the API call fails, the last valid catalog remains active; if a successful refresh returns no usable contracts, the logger disconnects and keeps polling rather than carrying yesterday's tickers forward. A changed ticker set clears every local book and quote-survival clock before reconnecting. No state crosses a contract roll.

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

## Rounding-path evidence

The source of truth is the raw Celsius observation. Every stored observation also includes:

- `temperature_f_round_cf`: `round(C → F)`;
- `temperature_f_round_c`: `F(round(C))`, retained as a mechanism diagnostic even though it is usually off the CLI integer grid;
- `temperature_f_round_cff`: `round(F(round(C)))`, the integer-grid Celsius-first hypothesis;
- `running_extreme_cf`, `running_extreme_c`, and `running_extreme_cff`, each computed independently over the climate day.

Every reconciliation stores all three candidate parsed values and agreement flags against the final settlement. The active signal path remains the explicitly configured rule path. For the current CLINYC example, `nearest_int` on the raw converted Fahrenheit value is the selected `cf` path unless reconciliation evidence supports a different mechanism.

**Do not trade while `cf` versus `cff` remains unresolved.** Signals collected during that period are research observations only. The venue is read-only regardless, but this is also the statistical go/no-go rule for any later execution work.

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
pytest -q tests/test_weather_research.py tests/test_weather_live.py tests/test_weather_rounding_candidates.py tests/test_weather_discovery.py
```

The focused suite covers unified YES pricing, ticker pagination and rolling, explicit strike-type mapping, bucket partition guards, intraday-safe certainty direction, bucket elimination, comparator-aware monotonicity, depth-aware fee rounding, error-derived thresholds, civil and fixed-standard climatological-day resets, full-day observation recomputation, receipt-time quote aging, three-path rounding persistence and reconciliation, decimal half-up boundaries, persistence, and sequence-gap poisoning.

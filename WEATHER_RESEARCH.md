# Structural Weather Research MVP

This package is a read-only falsification experiment. It does not place orders.

## Live path now included

- Authenticated Kalshi WebSocket connection using the recommended external API host.
- `use_yes_price: true` subscription and current fixed-point snapshot/delta parsing.
- Sequence-safe book state: any gap or uncertain top-level depletion discards local state and requests a fresh snapshot.
- NWS latest-observation polling.
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

Weather values are normalized to integer tenths using decimal half-up rounding, avoiding Python banker's rounding at exact boundaries.

## Validation

Run:

```bash
pytest -q tests/test_weather_research.py tests/test_weather_live.py
```

The focused tests cover unified YES pricing, intraday-safe certainty direction, bucket elimination, comparator-aware monotonicity, depth-aware fee rounding, error-derived thresholds, reconciliation cohorts, decimal half-up boundaries, persistence, and sequence-gap poisoning.

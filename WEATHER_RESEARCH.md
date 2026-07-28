# Structural Weather Research MVP

This package is a read-only falsification experiment. It does not place orders.

## Scope

1. Resolve the authenticated incentive-program endpoint and inspect active/upcoming weather programs.
2. Subscribe to weather order books with `use_yes_price: true`.
3. Emit realized cumulative-threshold YES signals.
4. Emit realized interval-bucket elimination NO signals as a first-class output.
5. Detect executable adjacent-strike monotonicity violations from unified YES prices, with fees calculated at actual executable depth.
6. Reconcile the parser against every station-day settlement, then report baseline, signal-conditioned, and would-have-filled error separately.
7. Set any future entry gap from the upper confidence bound on fill-conditioned mapping error, not from a fixed price floor.

## Hard boundaries

- Six-hour gate: valid data must be collecting by hour six.
- Twelve-hour total build/debug budget.
- No LLM probability forecasts, PCA, cross-platform feeds, generalized settlement compiler, or live order router.

## Required environment

- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PEM` or `KALSHI_PRIVATE_KEY_PATH`

`KalshiClient.discover_incentive_path()` authenticates against the likely documented endpoint and a compatibility fallback. One live call settles the literal REST path.

## EV rule

For a contract bought at price `p` cents and believed certain:

`EV cents = (100 - p) - 100 * fill_conditional_mapping_error - fee - slippage`

The reconciliation sample includes every station-day. Signal-days are tagged separately to measure boundary-induced selection bias. A row is additionally classified as `would_have_filled` only when the signal passes the configured minimum depth, quote-survival, and gap requirements; this cohort supplies the error term used by the EV rule.

Weather values are normalized to integer tenths using decimal half-up rounding, avoiding Python banker's rounding at exact boundaries.

## Validation

The focused unit tests cover unified YES pricing, intraday-safe threshold direction, daily-high/daily-low bucket elimination, comparator-aware executable monotonicity, depth-aware fee rounding, exact EV collapse, all-station-day and would-fill reconciliation, decimal half-up temperature boundaries, sequence-gap poisoning, and measured incentive denominator handling.

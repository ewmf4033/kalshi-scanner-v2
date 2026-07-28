# Structural Weather Research MVP

This package is a read-only falsification experiment. It does not place orders.

## Scope

1. Resolve the authenticated incentive-program endpoint and inspect active/upcoming weather programs.
2. Subscribe to weather order books with `use_yes_price: true`.
3. Emit realized cumulative-threshold YES signals.
4. Emit realized interval-bucket elimination NO signals as a first-class output.
5. Detect executable adjacent-strike monotonicity violations from unified YES prices.
6. Reconcile the parser against every station-day settlement, then report baseline and signal-conditioned error separately.
7. Set any future entry gap from the upper confidence bound on mapping error, not from a fixed price floor.

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

The reconciliation sample includes every station-day. Signal-days must also be tagged separately to measure boundary-induced selection bias.

## Validation

The new unit tests cover unified YES pricing, realized threshold signals, bucket elimination, executable monotonicity, exact EV collapse, 240-observation reconciliation power, and measured incentive denominator handling.

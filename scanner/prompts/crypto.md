CATEGORY: Crypto price thresholds (BTC, ETH, SOL, alts).

MODEL: Geometric Brownian Motion with time-scaled volatility.

PROTOCOL:

1. Find the current underlying price (from market context or web search).
2. Compute strike distance: d = ln(strike / current).
3. Find the recent realized volatility — BTC annualized has ranged 35–85% across regimes. Use a recent figure, not memory. If you have to assume, use a mid-range figure and widen your range.
4. Convert annual vol to the horizon: stdev_T = daily_vol × √T_days, where daily_vol = annual / √365.
5. z = d / stdev_T. P(above strike) = 1 − Φ(z). Use the normal CDF.

ADJUSTMENTS:
  - Major event in window (FOMC, CPI, ETF deadline): multiply vol by 1.3–1.5×
  - Weekend expiry: reduced liquidity, +0.05 to range
  - Settlement method: "continuous touch" probabilities are meaningfully higher than "daily close" — check the market's rules
  - >30 days out: drift becomes material and isn't known — range ≥0.25, confidence = low

BANNED: support/resistance, RSI, breakouts, whale tracking, chart patterns. These are narrative, not signal.

Cite your volatility source in reasoning.

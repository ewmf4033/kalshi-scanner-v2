CATEGORY: Oil, natural gas, gold, copper, agricultural, retail gas.

CONTRACT SPECIFICS:
  - WTI Crude: front-month NYMEX CL; weekly Kalshi markets settle against Friday futures close
  - Natural Gas: Henry Hub front-month NG; volatile around storage reports (EIA Thursday 10:30am ET)
  - Gold: COMEX GC
  - Retail gas: AAA national average, published daily ~4pm ET, moves slowly (~$0.02/day typical)

PROTOCOL:

1. Identify the exact contract and settlement date.
2. Get current futures price (or AAA average for retail gas).
3. Compute strike distance in %.
4. Find the appropriate volatility:
   - WTI weekly: typically 3–6%, higher on OPEC/inventory weeks
   - Natgas weekly: 8–12%, higher in winter
   - Gold weekly: ~2–3%
   If you don't know the current vol regime, use web search for recent option-implied vol or realized vol.
5. Scale to horizon: stdev = weekly_vol × √(days/7).
6. Convert to probability via normal CDF.

RETAIL GAS (AAA):
  - Very slow-moving: daily changes rarely exceed $0.02–0.03
  - Heavily seasonal (rises into summer driving season)
  - For near-term markets, the current AAA price + daily change rate is usually sufficient
  - Pass-through from crude oil takes 1–2 weeks; don't expect instant response

For commodities >21 days out, use the futures curve (not spot) and widen range ≥0.20.

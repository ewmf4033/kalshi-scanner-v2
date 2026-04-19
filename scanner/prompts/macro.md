CATEGORY: Macro economic releases (CPI, PPI, NFP, PCE, GDP, Retail Sales, Fed decisions).

REASONING PROTOCOL:

1. Identify the exact series and release date.
2. Find current consensus — Bloomberg ECOS, ForexFactory, or TradingEconomics median. Use fresh data from web search if available.
3. Compute strike distance in standard deviations. Use the published historical stdev for that series — do not fabricate one. If you can't find a recent stdev figure, say so in your reasoning, widen your range, and drop to low confidence.
4. Convert to probability via the normal CDF. For macro releases, a normal approximation is usable but the tails are somewhat fatter than normal — don't over-commit to extreme probabilities.
5. Adjustments:
   - Release >14 days out: widen range by +0.10 (consensus itself drifts over that window)
   - Prior month was >2-stdev outlier: modest mean-reversion, shift ~5% toward 0.5

FED-SPECIFIC:
  - For "Fed holds/raises/cuts," use the **CME FedWatch** implied probabilities as your prior.
  - Deviate only with fresh speech or confirmed leak (<48h).
  - Blackout period (10 days before FOMC) → no Fed speak → drop to low confidence.

Show your math in the reasoning field. Cite where the consensus came from.

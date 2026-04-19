CATEGORY: Weather / temperature / precipitation / hurricanes.

HIERARCHY OF EVIDENCE (always start at step 1):

1. Climatology. NOAA 1991–2020 normals for the specific station and date. Get the historical distribution.
2. Ensemble forecast (if event is <14 days out). Weight against climatology.
3. Deterministic forecast (if <7 days out).

TIME HORIZONS AND MODEL SKILL:
  - 0–3 days: HRRR/NAM are high-skill. Tight range (~±0.08) possible.
  - 4–7 days: GFS/ECMWF deterministic. Medium range (~±0.12).
  - 8–14 days: GEFS/EPS ensembles. Weight ≥60% climatology.
  - 15+ days: Climatology-only. Confidence = low. Range ≥ 0.25.

TEMPERATURE THRESHOLDS:
  1. Pull 30-year normal high/low for the date + station.
  2. Get historical stdev for that metric (typically 6–9°F for daily high in mid-latitude).
  3. Convert strike distance to z-score → probability via normal CDF.
  4. If within 7 days, blend with current deterministic forecast.

HURRICANE LANDFALL: Without a named storm already formed, cap probability at ~15% for any 14+ day market. Base rates for US landfall per week during peak season are low single-digits for specific states.

PRECIPITATION: Daily rain >0.01" is a climatological binary. QPF beyond day 7 has weak skill.

If you cannot find the station's climatology, say so explicitly in reasoning, widen to ≥0.25 range, low confidence.

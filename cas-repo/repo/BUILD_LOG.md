# Build log: Carbon-Aware Scheduler

A record of what was built, why, and what changed in each version.

---

## 31 Oct 2026, 10:45 IST: idea and research

- **Starting point.** A LinkedIn post arguing that software layers (carbon-aware scheduling, thermal optimisation, predictive health parsing) can cut AI data-center energy waste without new hardware.
- **Prior art.** YC-backed companies in this space include Voxel Energy (W26, solar-powered data centers), Terranox AI (W26, energy procurement), Dartboard Energy (W25, electricity-market analytics) and Elyos (building load shifting).
- **Decision.** Build carbon-aware workload scheduling first. It is the most measurable of the three ideas and needs no hardware access.

## 31 Oct 2026, 10:50 IST: v1.0 single-file prototype

- **Built.** `carbon_scheduler.py`, about 250 lines and standard library only:
  - fetches a forecast;
  - slides the job window across every start time before the deadline;
  - picks the lowest average;
  - logs to SQLite.
- **Sources.** The GB Carbon Intensity API (free, no key) and Electricity Maps (token), with a persistence fallback.
- **Test.** The sandbox could not reach external sites, so the logic was tested on a synthetic India-shaped forecast. Results were correct: it chose the midday solar window and respected a tight deadline.
- **Fix.** Fixture runs were mislabelled as source "uk"; changed to "file".

## 31 Oct 2026, 10:56 IST: live test

- Opened the GB API in the user's browser pane and captured the real 48 h forecast: 96 half-hour slots, 85–216 g/kWh. Saved as `data/live_uk_forecast.json`.
- **Results** for a 3 h job at 1.5 kW:

| Deadline | Decision | CO₂ cut |
|---|---|---|
| 12 h | Run now | 0% (the grid gets dirtier, so it correctly doesn't defer) |
| 24 h | Defer | 12% |
| 48 h | Defer to an early-Monday wind dip | 53% |

- Built an in-browser live view: the same algorithm in JavaScript, injected into the API page so it fetches live data with controls you can change.

## 31 Oct 2026, 11:20 IST: research for v2

| Fact | Value | Source |
|---|---|---|
| India grid emission factor | 0.727 tCO₂/MWh (FY 2023-24, weighted average) | CEA CO₂ Baseline Database |
| India hourly shape | Fossil generation about 125 GW at 1 pm, about 174 GW at 7 pm (H1 2026) | Ember, via Down To Earth |
| National ToD rule | C&I ≥10 kW from 1 Apr 2024: solar hours 10–20% cheaper, peak 10–20% dearer | Electricity (Rights of Consumers) Amendment Rules 2023 |
| Gujarat HTP-I tariff, from 1 Apr 2026 | Energy charge 400/420/430 p; peak 07–11 and 18–22 +45/+85 p; 11–17 −60 p | GERC tariff schedule |
| Electricity Maps free tier | Latest and 24 h history only, no forecast; base URL `api.electricitymaps.com/v3` | Electricity Maps docs |
| India data centers | About 2.2 GW (2025) → 12 GW (2030), about 40% CAGR | Wood Mackenzie, via Indian Infrastructure |

- **Key insight.** Gujarat's discount window (11:00–17:00) matches India's solar carbon dip, and its peak surcharge (18:00–22:00) matches the coal-heavy evening peak. Carbon and cost usually point to the same hours, so the product pitch is "save money, and the carbon comes free". (See the correction under 12:00 IST: they do not always agree.)

## 31 Oct 2026, 11:30 IST: v2.0 package

The single file was split into a package:

| Module | Purpose |
|---|---|
| `models.py` | `Point` and `Forecast` types; IST as a fixed offset (no `tzdata` needed on Windows) |
| `india.py` | Calibrated India model, scaled so the 24 h mean equals 727 g/kWh exactly |
| `tariffs.py` | Gujarat HTP-I (3 slabs), LTMD, the national ToD rule, and flat rates |
| `sources.py` | GB live, Electricity Maps (updated base URL; persistence forecast repeated to 48 h), India model, files, plus GB actuals for verification |
| `optimizer.py` | Carbon / cost / blend objectives; contiguous sliding window (exact); checkpointable split jobs via dynamic programming (exact, O(K·N)); multi-zone location shifting; honest run-now baseline |
| `audit.py` | Six SQLite tables plus a JSONL event stream; raw API payloads kept |
| `cli.py` | `plan`, `run` (block-wise launch, SIGTERM at block end), `report`, `audit`, `verify`, `export`, `profile`, `tariffs`; `--now` for what-if analysis |

**Fixes during build:**
- Negative "start in −0.4 h" when the current slot had already begun; now clamped to 0.
- `str.capitalize()` lowercased the zone code ("gb"); replaced with first-letter-only uppercasing.
- The audit view read the wrong columns; now uses named columns.
- The run's logged arguments included an internal function; now stripped.
- A job killed by SIGTERM stayed "scheduled"; now logged as "cancelled".
- An execution test waited for a cleaner slot about 1 h away. That was correct behaviour, so the test was switched to a forecast where now is best.

**Tests:** 14 automated tests, all passing:
- the sliding window matches brute force on 200 random cases;
- the split-job DP matches brute force on 60 cases;
- splitting is never worse than running in one block;
- deadline respected; zone selection; India mean = 727; tariff rates by hour; timestamp parsing; full audit trail written.

**Headline results** (India model, Gujarat HTP-I, 4 h job at 8 kW, 24 h deadline):

| Submitted | Moved to | CO₂ cut | Energy charge saved |
|---|---|---|---|
| 18:00 | 11:00 next day | 29% (7.6 kg) | ₹46.40 per run |
| 22:00 | 11:00 next day | 29% | ₹19.20 |
| 07:00 | 11:00 | 17% | ₹46.40 |
| 12:00 | Run now | 0% | 0 |

- **8 h job:** one block saves 25% and ₹42.40; two blocks save 29% and ₹65.60.
- **Scaled to 1 MW** of flexible load moved from the evening peak, 4 h/day: about 345 t CO₂/year and about ₹21 lakh/year in energy charges.

## 31 Oct 2026, 12:00 IST: presentation package and a correction

- **Demo dashboard** (shareable page): an interactive what-if over 72 precomputed scenarios (submit hour × job length), the India carbon and tariff curves with the chosen window highlighted, the per-MW scale-up, split jobs, and the live GB test. Data exported from the engine as `planner`, `india_hourly`, `split_jobs`, `gb_live` and `gb_decisions`.
- **Technical whitepaper** (shareable doc): method, India model calibration, Gujarat tariffs, results, audit trail, limits, roadmap and sources.
- **Pitch deck**, 11 slides: problem, insight, solution, result, scenarios, technology, live test, scale, roadmap, ask.
- **Correction.** The claim "carbon and cost objectives choose the same window" was tested across all 72 combinations (2/4/8 h jobs × 24 submit hours):
  - they differed in 43;
  - every discount hour costs the same, so cost may start an hour earlier than the carbon optimum;
  - long jobs' cleanest windows can spill into the morning peak;
  - the `blend` objective matched the best rupee saving with equal or lower CO₂ in 68 of 72.

  The whitepaper states this precisely.

## 31 Oct 2026, 13:00 IST: ML forecaster and real-data validation

**Data found:**
- **HPE SustainCluster** (MIT licence; the user's fork was empty, so the upstream repo was cloned):
  - Western India (IN-WE) hourly carbon intensity, 2021–2024: 35,064 hours from Electricity Maps, 100% flagged "estimated".
  - India power-exchange price, INR/MWh, 2020–2024, from Grid-India/POSOCO.
  - Mumbai hourly weather, 2020–2024, from Open-Meteo.
- **Grid-India all-India hourly data** (Mendeley, CC BY 4.0): demand, solar and wind, Jan 2024 – Jun 2025, 12,984 hours. Parsed in the user's browser, since the sandbox cannot reach Mendeley.

**Finding.** The Electricity Maps estimate is nearly flat: a 28 g/kWh daily swing, about 4%. The measured Grid-India data shows midday solar of about 58 GW (2025). Assuming 20 GW of clean hydro and nuclear, fossil share is about 60% at 13:00 vs about 85% at 19:00. That confirms the original India model's shape (about 29%) and shows the estimate misses the solar effect.

**Measured savings on real Grid-India data.** 539 days, 4 h job, 24 h deadline, scheduled from yesterday's pattern, fossil-share reduction:

| Submitted | 0 GW clean assumed | 20 GW | 30 GW | Best possible (20 GW) |
|---|---|---|---|---|
| 18:00 | 22.1% | 24.2% | 25.4% | 24.4% |
| 00:00 | 21.9% | 22.9% | 23.4% | 23.0% |
| 06:00 | 16.2% | 17.8% | 18.7% | 18.0% |
| 12:00 | about 0% | about 0% | about 0% | about 0% |

The pitch figure is updated from 29% (model) to about 24% (measured).

**ML model (`cas/ml.py`):**
- **Method:** HistGradientBoostingRegressor, direct multi-horizon (h = 1–48), with a residual target (the change from the latest same-hour value).
- **Features:** 14, covering calendar, history and weather.
- **Training:** 2021–2023 for carbon (626k rows), 2020–2023 for price (835k rows). About 20 s each.
- **Test year:** 2024, held out.

| | Carbon MAE (g/kWh) | Price MAE (Rs/kWh) | Price saving captured |
|---|---|---|---|
| ML | 6.0 | 1.065 | 94.5% |
| Yesterday's pattern | 5.1 | 1.103 | 90.9% |
| Hour × month average | 12.5 | 1.575 | 93.7% |
| CAS v2 static India curve | 95.7 | n/a | n/a |

- Carbon: the ML has a slightly higher hour-by-hour error than "same hour yesterday", but makes the best scheduling decisions: 95.8% of the achievable saving captured.

**Integration:**
- `--source ml` (carbon forecast), `--tariff ml-price` (price forecast as a dynamic tariff), and a new `cas forecast` command.
- `requirements-ml.txt`; the core stays standard-library only.

**Fixes during build:**
- IST-stamped price hours fall on :30 UTC; forecasts now anchor on the last reading.
- Fixed a baseline timestamp mismatch that made persistence look useless.
- Fixed a duplicate `name` argument in the audit event.
- The verdict now says "more CO₂" when the cost objective trades carbon for price.

**Tests:** 19, all passing. New ones cover no future leakage, same-hour lag correctness, and the ML beating the hour average out of sample on real data.

## Known limits and next steps

- Replace the India model with live regional data (Electricity Maps paid tier, or Grid-India data).
- Use marginal emission signals in place of average intensity.
- Add demand charges and FPPAS to the cost model.
- Integrate with Kubernetes, Slurm and Airflow.
- Export in a BRSR-ready reporting format.

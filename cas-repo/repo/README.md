# Carbon-Aware Scheduler (CAS)

Software that moves flexible AI and batch workloads into India's cleanest and cheapest hours, with no new hardware and no change to the job.

**Headline results**

| Result | Figure | Basis |
|---|---|---|
| Fossil-power cut, evening job moved to midday | **~24%** | Real Grid-India data, 539 days (Jan 2024 – Jun 2025) |
| Energy-charge cut, same move | **29%** | Gujarat HTP-I tariff (GERC, from 1 Apr 2026) |
| Exchange-price saving with the ML forecaster | **32.5%** (94.5% of the best possible) | Held-out 2024 Indian power-exchange prices |
| Live test, Great Britain grid, 48 h deadline | **53%** | National Grid ESO live forecast, 31 Oct 2026 |

## Repository map

| Path | What it is |
|---|---|
| `cas/` | The scheduler: optimiser, India carbon model, tariffs, data sources, ML forecaster, audit log, CLI |
| `ml/train_eval.py` | Trains the ML models and tests them on held-out 2024 data |
| `models/` | Trained models and their test results (`results.json`) |
| `tests/` | 19 automated tests (`python -m unittest discover -s tests -v`) |
| `data/` | Captured live GB forecast and headline result numbers |
| `docs/CAS_Technical_Whitepaper.pdf` | Full technical whitepaper, including validation, ML, costs and glossary |
| `docs/deck/` | Pitch deck slide source (12 slides, HTML) |
| `docs/dashboard-data/` | Datasets and page behind the demo dashboard |
| `prototype/` | The original v1 single-file prototype |
| `BUILD_LOG.md` | Every step, finding, fix and correction, in order |


Shifts flexible compute (AI training, fine-tuning, batch inference, ETL, backups) to the hours when the grid is cleanest and electricity is cheapest. It reports the CO₂ and rupees saved and keeps a complete audit trail of every decision.

Python 3.9+ standard library only. There's nothing to install.

## Quick start

```bash
cd cas-v2
python -m cas profile                                   # India carbon curve vs Gujarat HT tariff
python -m cas plan --hours 4 --kw 8 --tariff gj-htp1    # best time today for a 4 h, 8 kW job
python -m cas plan --source gb --hours 3 --kw 1.5       # live Great Britain grid (no key)
python -m cas report                                    # everything logged so far
python -m unittest discover -s tests -v                 # 14 tests
```

## What it can do

| Capability | How |
|---|---|
| **Time shifting** | Finds the cleanest window before your deadline (`--deadline`) |
| **Carbon + cost** | `--objective carbon`, `cost`, or `blend --weight 0.7` |
| **Indian tariffs** | `--tariff gj-htp1` (Gujarat HT, official FY 2026-27 rates), `gj-ltmd`, `india-tod`, `flat:6.5` |
| **Checkpointable jobs** | `--chunks 2` splits a job into blocks placed in separate clean windows (exact dynamic programming) |
| **Location shifting** | Repeat `--source` to compare zones, e.g. `--source india --source gb` |
| **What-if analysis** | `--now 2026-10-31T18:00+05:30` plans as if submitted at that time |
| **Run the job** | `python -m cas run ... -- python train.py` waits, launches, pauses at block ends |
| **Audit** | `audit <run_id>`, `report`, `export`, `verify` |

## Data sources

| `--source` | Data | Key |
|---|---|---|
| `india` (default) | India model: CEA FY 2023-24 mean of 727 g/kWh, hourly shape from Ember 2026 fossil-generation data | None |
| `gb` | National Grid ESO live 48 h forecast | None |
| `em:IN-WE` | Electricity Maps live data for any zone. Free keys give 24 h history, which CAS turns into a next-day forecast | `ELECTRICITYMAPS_TOKEN` |
| `file:path.json` | Saved forecast, for repeatable demos | None |

## ML forecasting (optional)

Gradient-boosting models forecast the next 48 hours of grid carbon intensity (Western India) and power-exchange price (India). They are trained on public data from HPE's SustainCluster repository and tested on 2024, a year they never saw.

```bash
pip install -r requirements-ml.txt
git clone --depth 1 https://github.com/HewlettPackard/sustain-cluster
python ml/train_eval.py --repo sustain-cluster          # trains, tests on 2024, saves models/
export CAS_DATA_REPO=sustain-cluster
python -m cas forecast --now 2024-11-20T18:00+05:30     # 48 h carbon + price forecast
python -m cas plan --source ml --tariff ml-price --objective cost --now 2024-11-20T18:00+05:30 --hours 4 --kw 8
```

Results on 2024, 4 h job, 24 h deadline, 1,428 cases (see `models/results.json`):

| Signal | Saving vs running now (ML) | Best possible | Share captured: ML / yesterday's pattern |
|---|---|---|---|
| Exchange price | 32.5% | 35.7% | 94.5% / 90.9% |
| Carbon (Electricity Maps estimate) | 2.3% | 2.4% | 95.8% / 94.5% |

The carbon estimate data is unusually flat. On real Grid-India generation data (Jan 2024 – Jun 2025), the same evening job cuts fossil share by 22–25%; see `BUILD_LOG.md`.

## The audit trail

Every command writes to two places:

- **`cas_audit.db`** (SQLite) has six tables:
  - `runs`: every invocation, with its full arguments.
  - `forecasts`: every dataset fetched, including the raw API payload.
  - `candidates`: every option the optimiser scored.
  - `decisions`: the chosen plan, baseline, savings and reason.
  - `executions`: scheduled vs actual start, exit code and duration.
  - `verifications`: forecast vs measured intensity.
- **`cas_events.jsonl`** is an append-only, human-readable event stream: `run_started` → `forecast_fetched` → `candidates_scored` → `decision_made` → `job_started` → `job_finished`.

`python -m cas audit <run_id>` reconstructs any decision end to end.

## Checkpointable jobs (`--chunks`)

With `--chunks N`, `run` launches your command at each block start and sends SIGTERM at each block end. Your job must save a checkpoint on SIGTERM and resume from it on the next launch, which most ML training loops already support.

## Honest limits

- The India source is a calibrated typical-day model, not live data. Use `em:IN-WE` with a token for live India data.
- Savings use average grid intensity. The true effect of shifting load depends on the marginal plant.
- Tariff presets cover energy charges only. They exclude duty, taxes, demand charges and fuel surcharge (FPPAS).

See `BUILD_LOG.md` for how and why each part was built.

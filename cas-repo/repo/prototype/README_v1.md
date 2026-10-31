# Carbon-Aware Scheduler

Picks the cleanest time on the electricity grid to run a flexible job (model fine-tuning, batch inference, backups, data pipelines). It also tells you how much CO₂ that saves compared with running it right now, and logs every decision so you can total up the savings.

It uses real grid data, with no simulation, and needs only Python 3.9 or later. There's nothing to install.

## Data sources

| Source | Region | Key needed | What you get |
|---|---|---|---|
| `uk` | Great Britain | No | 48-hour forecast, 30-min slots |
| `em` | Any zone, e.g. `IN-WE` (Western India), `IN-NO`, `DE`, `US-CAL-CISO` | Free token | Forecast if your plan includes it; otherwise a "same hour yesterday" forecast built from real 24-hour history |

For India, get a free token at app.electricitymaps.com, then:

```bash
# Windows (PowerShell)
$env:ELECTRICITYMAPS_TOKEN="your_token"
# Mac / Linux
export ELECTRICITYMAPS_TOKEN=your_token
```

Start with `--source uk` to see it working on live data straight away, with no signup.

## Usage

**1. Plan: when should this job run?**
```bash
python carbon_scheduler.py plan --source uk --hours 3 --kw 1.5 --deadline 24 --job llm-finetune
python carbon_scheduler.py plan --source em --zone IN-WE --hours 2 --kw 0.8
```

**2. Run: wait for the clean window, then launch the job**
```bash
python carbon_scheduler.py run --source uk --hours 2 --kw 1.2 --deadline 12 --job finetune -- python train.py
```

**3. Report: total CO₂ avoided so far**
```bash
python carbon_scheduler.py report
```

## Options

- `--hours`: how long the job takes.
- `--kw`: average power draw. A single GPU workstation is about 0.4–1 kW; an 8-GPU server is about 6–10 kW.
- `--deadline`: the job must finish within this many hours. A tighter deadline gives fewer options and less saving.
- `--job`: a name for the log.
- `--fixture file.json`: offline testing with a saved forecast (`[{"start": iso, "gco2": n}, ...]`).

Decisions are stored in `carbon_log.db` (SQLite). Set `CARBON_DB` to change the path.

## How it works

1. It fetches the grid's carbon-intensity forecast (gCO₂ per kWh, per time slot).
2. It slides a window the length of your job across every possible start time before the deadline.
3. It picks the window with the lowest average intensity.
4. The saving is (run-now intensity − best intensity) × kWh ÷ 1000, in kg CO₂.

## Honest limits

- The figure is CO₂ that would have been emitted at the time the job ran, using average grid intensity. Marginal emissions, which is what a load shift actually changes, can differ.
- The Indian grid is coal-heavy, so its intensity is high most of the day. The midday solar dip is where most of the saving is.
- The persistence fallback assumes tomorrow looks like today. That's reasonable for solar timing but misses weather changes.

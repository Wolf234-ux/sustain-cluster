"""
Command-line interface.

  python -m cas plan    find the best schedule and log it
  python -m cas run     wait for the best window(s), run your command, log everything
  python -m cas report  totals and recent decisions
  python -m cas audit   full record of one run (forecast, every option scored, decision, execution)
  python -m cas verify  compare forecast vs measured intensity for past GB runs
  python -m cas export  dump the audit tables to CSV
  python -m cas profile show the India carbon curve next to a tariff, hour by hour
  python -m cas tariffs list built-in tariffs
"""

import argparse
import csv
import signal
import sqlite3
import json
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timedelta

from . import VERSION
from .audit import Audit
from .india import hourly_profile
from .models import IST, UTC, parse_ts
from .optimizer import optimise
from .sources import gb_actuals, get_forecast
from .tariffs import PRESETS, get_tariff


def _clean(args):
    return {k: v for k, v in vars(args).items() if k != "func"}


def fmt(dt):
    return dt.astimezone().strftime("%a %d %b %H:%M")


def rs(v):
    return "-" if v is None else f"Rs {v:,.2f}"


# ---------------------------------------------------------------------------
def build_plan(args, audit):
    now = parse_ts(args.now) if getattr(args, "now", None) else datetime.now(UTC)
    sources = args.source or ["india"]
    forecasts = []
    for s in sources:
        if s == "ml":
            from .ml_bridge import ml_forecast
            fc = ml_forecast(now, getattr(args, "repo", None))
        else:
            fc = get_forecast(s, now)
        audit.forecast(fc)
        forecasts.append(fc)
    if args.tariff == "ml-price":
        from .ml_bridge import ml_price_tariff
        tariff = ml_price_tariff(now, getattr(args, "repo", None))
        audit.event("tariff_forecast", tariff_name=tariff.name, note=tariff.note,
                    min_rs=min(tariff.rates.values()), max_rs=max(tariff.rates.values()))
    else:
        tariff = get_tariff(args.tariff)
    plan = optimise(forecasts, now, args.hours, args.kw, args.deadline, tariff,
                    args.objective, args.weight, args.chunks)
    return plan, forecasts, tariff, now


def print_plan(plan, forecasts, tariff, job):
    print(f"\n  Carbon-Aware Scheduler v{VERSION}")
    print(f"  Job        : {job}  |  {plan.hours:g} h x {plan.kw:g} kW = {plan.kwh:g} kWh  |  objective: {plan.objective}")
    for fc in forecasts:
        print(f"  Data       : [{fc.zone}] {fc.method}")
    if tariff:
        print(f"  Tariff     : {tariff.name}")
    print(f"\n  {'':<12}{'gCO2/kWh':>10}{'kg CO2':>10}{'Cost':>14}")
    print(f"  {'Run now':<12}{plan.baseline_gco2:>10.0f}{plan.baseline_kg:>10.3f}{rs(plan.baseline_rs):>14}")
    avg = plan.kg * 1000 / plan.kwh
    print(f"  {'Best plan':<12}{avg:>10.0f}{plan.kg:>10.3f}{rs(plan.rs):>14}")
    print("\n  Schedule:")
    for i, b in enumerate(plan.blocks, 1):
        print(f"    {i}. [{b.zone}] {fmt(b.start)} -> {fmt(b.end)}   ({b.avg_gco2:.0f} g/kWh)")
    print(f"\n  Verdict    : {plan.reason}")
    print(f"  Options scored: {len(plan.candidates)}\n")


def cmd_plan(args):
    a = Audit()
    a.start_run("plan", _clean(args))
    try:
        plan, fcs, tariff, _ = build_plan(args, a)
    except Exception as e:
        a.error(str(e)); raise
    a.decision(plan, args.job, tariff.name if tariff else None, args.deadline, args.chunks, "planned")
    if args.json:
        print(json.dumps({"run_id": a.run_id, "reason": plan.reason, "kg_saved": plan.kg_saved,
                          "pct_saved": plan.pct_saved, "rs_saved": plan.rs_saved,
                          "blocks": [{"zone": b.zone, "start": b.start.isoformat(), "end": b.end.isoformat()}
                                     for b in plan.blocks]}, indent=2))
    else:
        print_plan(plan, fcs, tariff, args.job)
        print(f"  Logged as run {a.run_id}  (see: python -m cas audit {a.run_id})\n")


def _sleep_until(t):
    while True:
        left = (t - datetime.now(UTC)).total_seconds()
        if left <= 0:
            return
        time.sleep(min(300, max(1, left)))


def cmd_run(args):
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        sys.exit("Give the command to run after --, e.g.  -- python train.py")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    a = Audit()
    a.start_run("run", _clean(args))
    plan, fcs, tariff, _ = build_plan(args, a)
    a.decision(plan, args.job, tariff.name if tariff else None, args.deadline, args.chunks, "scheduled")
    print_plan(plan, fcs, tariff, args.job)
    print(f"  Run id {a.run_id}. Ctrl+C cancels.\n")
    try:
        overall = 0
        for i, b in enumerate(plan.blocks, 1):
            if (b.start - datetime.now(UTC)).total_seconds() > 30:
                print(f"  Waiting for block {i} at {fmt(b.start)} ...")
                a.event("waiting", block=i, until=b.start)
                _sleep_until(b.start)
            print(f"  Block {i}: launching {' '.join(cmd)}")
            row = a.exec_start(cmd, i, b.start)
            t0 = time.time()
            proc = subprocess.Popen(cmd)
            status = "completed"
            if len(plan.blocks) > 1:
                limit = (b.end - datetime.now(UTC)).total_seconds()
                try:
                    proc.wait(timeout=max(1, limit))
                except subprocess.TimeoutExpired:
                    proc.terminate()   # job must checkpoint on SIGTERM and resume next block
                    proc.wait()
                    status = "paused_at_block_end"
            else:
                proc.wait()
            code = proc.returncode
            if status == "completed" and code != 0:
                status = "failed"
            a.exec_end(row, code, time.time() - t0, status)
            print(f"  Block {i} ended ({status}, exit {code}).")
            overall = code
            if status == "completed" and len(plan.blocks) > 1 and code == 0:
                print("  Job finished early; skipping remaining blocks.")
                break
        a.set_action("executed")
        print(f"\n  Done. {plan.kg_saved:.3f} kg CO2 avoided"
              + (f", {rs(plan.rs_saved)} saved" if plan.rs_saved is not None else "") + ".\n")
        sys.exit(overall if overall and overall > 0 else 0)
    except KeyboardInterrupt:
        a.set_action("cancelled")
        sys.exit("\n  Cancelled and logged.")


def cmd_report(args):
    a = Audit()
    c = a.con
    rows = c.execute("""SELECT r.created_at, d.job, d.objective, d.zone, d.kwh, d.baseline_gco2,
        d.kg*1000.0/d.kwh, d.kg_saved, d.rs_saved, d.action, d.run_id
        FROM decisions d JOIN runs r USING(run_id) ORDER BY r.created_at DESC LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("No decisions logged yet."); return
    print(f"\n  {'When (UTC)':<17}{'Job':<16}{'Obj':<7}{'Zone':<6}{'kWh':>7}{'Now':>6}{'Best':>6}{'kg saved':>10}{'Rs saved':>10}  {'Action':<10}Run")
    for r in rows:
        print(f"  {r[0][:16]:<17}{(r[1] or '')[:15]:<16}{r[2]:<7}{r[3]:<6}{r[4]:>7.1f}{r[5]:>6.0f}{r[6]:>6.0f}"
              f"{r[7]:>10.3f}{'-' if r[8] is None else f'{r[8]:.2f}':>10}  {r[9]:<10}{r[10]}")
    t = c.execute("""SELECT COUNT(*), COALESCE(SUM(kwh),0), COALESCE(SUM(kg_saved),0), COALESCE(SUM(rs_saved),0)
                     FROM decisions WHERE action='executed'""").fetchone()
    p = c.execute("SELECT COUNT(*), COALESCE(SUM(kg_saved),0) FROM decisions").fetchone()
    print(f"\n  Executed: {t[0]} jobs | {t[1]:.1f} kWh | {t[2]:.3f} kg CO2 avoided | Rs {t[3]:,.2f} saved")
    print(f"  All decisions incl. plans: {p[0]} | potential {p[1]:.3f} kg CO2\n")


def cmd_audit(args):
    c = Audit().con
    run = c.execute("SELECT * FROM runs WHERE run_id=?", (args.run_id,)).fetchone()
    if not run:
        sys.exit(f"No run {args.run_id}")
    print(f"\n  Run {run[0]}  |  {run[1]}  |  v{run[2]}  |  command: {run[3]}  |  host: {run[5]}")
    print(f"  Arguments: {run[4]}")
    print("\n  Forecasts fetched:")
    for f in c.execute("""SELECT zone, source, method, n_points, step_h, min_gco2, max_gco2, mean_gco2
                          FROM forecasts WHERE run_id=?""", (args.run_id,)):
        print(f"    [{f[0]}] {f[2]}  |  {f[3]} slots x {f[4]:g} h  |  min {f[5]:.0f} / mean {f[7]:.0f} / max {f[6]:.0f} g/kWh")
    n = c.execute("SELECT COUNT(*) FROM candidates WHERE run_id=?", (args.run_id,)).fetchone()[0]
    print(f"\n  Options scored: {n}. Best 5 by score:")
    for r in c.execute("""SELECT zone, start, avg_gco2, kg, rs, score FROM candidates WHERE run_id=?
                          ORDER BY score LIMIT 5""", (args.run_id,)):
        print(f"    [{r[0]}] {r[1][:16]}  {r[2]:>6.0f} g/kWh  {r[3]:.4f} kg  {'' if r[4] is None else f'Rs {r[4]:.2f}'}")
    c.row_factory = sqlite3.Row
    d = c.execute("SELECT * FROM decisions WHERE run_id=?", (args.run_id,)).fetchone()
    c.row_factory = None
    if d:
        print(f"\n  Decision ({d['action']}): {d['reason']}")
        print(f"    Baseline [{d['baseline_zone']}] {fmt(datetime.fromisoformat(d['baseline_start']))}  {d['baseline_gco2']:.0f} g/kWh  "
              f"{d['baseline_kg']:.4f} kg  {'' if d['baseline_rs'] is None else 'Rs %.2f' % d['baseline_rs']}")
        for b in json.loads(d['blocks_json']):
            print(f"    Plan     [{b['zone']}] {fmt(datetime.fromisoformat(b['start']))} -> {fmt(datetime.fromisoformat(b['end']))}  {b['avg_gco2']:.0f} g/kWh")
        print(f"    Saved {d['kg_saved']:.4f} kg CO2 ({d['pct_saved']:.1f}%)"
              + ('' if d['rs_saved'] is None else f", Rs {d['rs_saved']:.2f}"))
    for e in c.execute("""SELECT block_no, scheduled_start, actual_start, ended_at, exit_code, duration_s, status
                          FROM executions WHERE run_id=?""", (args.run_id,)):
        print(f"  Execution block {e[0]}: scheduled {e[1][:16]}, started {e[2][:16]}, ended {str(e[3])[:16]},"
              f" exit {e[4]}, {e[5]} s, {e[6]}")
    for v in c.execute("SELECT verified_at, forecast_gco2, actual_gco2, error_pct FROM verifications WHERE run_id=?",
                       (args.run_id,)):
        print(f"  Verified {v[0][:16]}: forecast {v[1]:.0f} vs actual {v[2]:.0f} g/kWh ({v[3]:+.1f}%)")
    print()


def cmd_verify(args):
    a = Audit()
    a.start_run("verify", _clean(args))
    rows = a.con.execute("""SELECT d.run_id, d.blocks_json, d.kwh, d.kg FROM decisions d
        WHERE d.action='executed' AND d.zone='GB'
        AND d.run_id NOT IN (SELECT run_id FROM verifications)""").fetchall()
    done = 0
    for run_id, blocks_json, kwh, kg in rows:
        blocks = json.loads(blocks_json)
        end = max(datetime.fromisoformat(b["end"]) for b in blocks)
        if end > datetime.now(UTC):
            continue
        acts = [gb_actuals(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in blocks]
        if None in acts:
            continue
        actual = sum(acts) / len(acts)
        forecast = kg * 1000 / kwh
        a.verification(run_id, round(forecast, 1), round(actual, 1), round(actual * kwh / 1000, 5))
        print(f"  {run_id}: forecast {forecast:.0f} vs actual {actual:.0f} g/kWh")
        done += 1
    print(f"  Verified {done} run(s). India-model and Electricity Maps runs are not verified automatically.")


def cmd_export(args):
    c = Audit().con
    for table in ("runs", "forecasts", "candidates", "decisions", "executions", "verifications"):
        cur = c.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        if table == "forecasts":
            cols = [x for x in cols if x != "raw_json"]
            cur = c.execute(f"SELECT {', '.join(cols)} FROM forecasts")
        path = f"{args.prefix}{table}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(cols); w.writerows(cur.fetchall())
        print(f"  wrote {path}")


def cmd_profile(args):
    prof = hourly_profile()
    tariff = get_tariff(args.tariff)
    print(f"\n  India hourly carbon intensity (model) vs {tariff.name}")
    print(f"  {'IST':<6}{'gCO2/kWh':>9}  {'Rs/kWh':>7}  carbon")
    base = datetime(2026, 1, 1, tzinfo=IST)
    for h in range(24):
        r = tariff.rate(base.replace(hour=h))
        bar = "#" * int(prof[h] / 25)
        print(f"  {h:02d}:00{prof[h]:>9.0f}  {r:>7.2f}  {bar}")
    print(f"\n  Mean {sum(prof)/24:.0f} g/kWh (CEA FY24 = 727). Cleanest hour {min(range(24), key=lambda h: prof[h]):02d}:00, "
          f"dirtiest {max(range(24), key=lambda h: prof[h]):02d}:00.\n")


def cmd_forecast(args):
    from .ml_bridge import ml_forecast, ml_price_tariff
    now = parse_ts(args.now) if args.now else datetime.now(UTC)
    fc = ml_forecast(now, args.repo)
    tr = ml_price_tariff(now, args.repo)
    print(f"\n  ML forecast as of {fmt(now)} (IST)  |  {fc.method}")
    print(f"  {'Hour':<17}{'gCO2/kWh':>10}{'Rs/kWh':>9}")
    for p in fc.points[:args.hours]:
        print(f"  {fmt(p.start):<17}{p.gco2:>10.0f}{tr.rate(p.start):>9.2f}")
    print()


def cmd_tariffs(args):
    for k, t in PRESETS.items():
        print(f"  {k:<15} {t.name}\n  {'':<15} {t.note}")
    print("  flat:<rs>       a flat rate, e.g. flat:6.5")


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="cas", description=f"Carbon-Aware Scheduler v{VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def job_args(p):
        p.add_argument("--source", action="append",
                       help="gb | india | ml | em:ZONE | file:PATH. Repeat to compare zones. Default: india")
        p.add_argument("--repo", help="path to the public SustainCluster data (for ml / ml-price); or set CAS_DATA_REPO")
        p.add_argument("--hours", type=float, default=3, help="job length in hours")
        p.add_argument("--kw", type=float, default=1.0, help="average power draw in kW")
        p.add_argument("--deadline", type=float, default=24, help="job must finish within N hours")
        p.add_argument("--tariff", default="", help="gj-htp1 | gj-htp1-small | gj-htp1-large | gj-ltmd | india-tod | ml-price | flat:<rs>")
        p.add_argument("--objective", default="carbon", choices=["carbon", "cost", "blend"])
        p.add_argument("--weight", type=float, default=0.5, help="blend: carbon weight 0-1")
        p.add_argument("--chunks", type=int, default=1, help="split a checkpointable job into N blocks")
        p.add_argument("--job", default="batch-job", help="name for the log")
        p.add_argument("--now", help="what-if: plan as if the current time were this ISO time, e.g. 2026-10-31T18:00+05:30")

    p = sub.add_parser("plan", help="find the best schedule"); job_args(p)
    p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_plan)
    p = sub.add_parser("run", help="wait and run a command at the best time"); job_args(p)
    p.add_argument("command", nargs=argparse.REMAINDER); p.set_defaults(func=cmd_run)
    p = sub.add_parser("report"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_report)
    p = sub.add_parser("audit"); p.add_argument("run_id"); p.set_defaults(func=cmd_audit)
    p = sub.add_parser("verify"); p.set_defaults(func=cmd_verify)
    p = sub.add_parser("export"); p.add_argument("--prefix", default="cas_"); p.set_defaults(func=cmd_export)
    p = sub.add_parser("profile"); p.add_argument("--tariff", default="gj-htp1"); p.set_defaults(func=cmd_profile)
    p = sub.add_parser("tariffs"); p.set_defaults(func=cmd_tariffs)
    p = sub.add_parser("forecast", help="print the ML carbon and price forecast")
    p.add_argument("--now"); p.add_argument("--repo"); p.add_argument("--hours", type=int, default=24)
    p.set_defaults(func=cmd_forecast)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except urllib.error.URLError as e:
        sys.exit(f"Network error reaching the grid API: {e}")
    except (ValueError, RuntimeError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()

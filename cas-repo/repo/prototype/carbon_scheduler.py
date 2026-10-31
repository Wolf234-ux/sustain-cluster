#!/usr/bin/env python3
"""
Carbon-Aware Scheduler
======================
Finds the cleanest time window on the electricity grid to run a flexible
(non-urgent) job, estimates the CO2 saved versus running it now, and can
wait and launch the job automatically. Every decision is logged to SQLite.

Real data sources (no simulation):
  uk  - National Grid ESO Carbon Intensity API. Free, no key, 48h forecast.
  em  - Electricity Maps API (any zone, e.g. IN-WE = Western India).
        Needs a free token in the env var ELECTRICITYMAPS_TOKEN.
        Uses the forecast endpoint if your plan has it; otherwise falls back
        to a "same hour yesterday" forecast built from real 24h history.

Standard library only (Python 3.9+). No pip install needed.

Examples:
  python carbon_scheduler.py plan --source uk --hours 3 --kw 1.5 --deadline 24
  python carbon_scheduler.py plan --source em --zone IN-WE --hours 2 --kw 0.8
  python carbon_scheduler.py run  --source uk --hours 2 --kw 1.2 --deadline 12 \
        --job "fine-tune" -- python train.py
  python carbon_scheduler.py report
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("CARBON_DB", "carbon_log.db")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Point:
    start: datetime          # UTC, start of the interval
    gco2: float              # grams CO2 per kWh during this interval


def _parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _get_json(url: str, headers: dict = None, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "carbon-aware-scheduler/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def fetch_uk(now: datetime) -> list:
    """Great Britain national forecast, 30-min resolution, ~48h ahead."""
    frm = now.strftime("%Y-%m-%dT%H:%MZ")
    data = _get_json(f"https://api.carbonintensity.org.uk/intensity/{frm}/fw48h")
    pts = []
    for row in data.get("data", []):
        val = row["intensity"].get("forecast") or row["intensity"].get("actual")
        if val is not None:
            pts.append(Point(_parse_ts(row["from"]), float(val)))
    return pts


def fetch_em(zone: str, now: datetime) -> tuple:
    """Electricity Maps. Returns (points, method_note)."""
    token = os.environ.get("ELECTRICITYMAPS_TOKEN")
    if not token:
        sys.exit("Set ELECTRICITYMAPS_TOKEN first (free at app.electricitymaps.com).")
    h = {"auth-token": token}
    base = "https://api.electricitymap.org/v3/carbon-intensity"

    # 1) Real forecast, if the plan includes it
    try:
        data = _get_json(f"{base}/forecast?zone={zone}", h)
        pts = [Point(_parse_ts(r["datetime"]), float(r["carbonIntensity"]))
               for r in data.get("forecast", []) if r.get("carbonIntensity") is not None]
        if pts:
            return pts, "Electricity Maps forecast"
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            raise

    # 2) Fallback: last 24h of real measurements, shifted +24h
    #    ("tomorrow at 2pm will look like today at 2pm" - a persistence forecast)
    data = _get_json(f"{base}/history?zone={zone}", h)
    pts = [Point(_parse_ts(r["datetime"]) + timedelta(hours=24), float(r["carbonIntensity"]))
           for r in data.get("history", []) if r.get("carbonIntensity") is not None]
    if not pts:
        sys.exit(f"No data returned for zone {zone}.")
    return pts, "persistence forecast from real 24h history"


def load_fixture(path: str) -> list:
    """Offline testing: JSON list of {"start": iso, "gco2": number}."""
    with open(path) as f:
        rows = json.load(f)
    return [Point(_parse_ts(r["start"]), float(r["gco2"])) for r in rows]


def get_forecast(args, now: datetime) -> tuple:
    if args.fixture:
        return load_fixture(args.fixture), f"fixture {args.fixture}"
    if args.source == "uk":
        return fetch_uk(now), "GB Carbon Intensity API forecast"
    if args.source == "em":
        return fetch_em(args.zone, now)
    sys.exit(f"Unknown source {args.source}")


# ---------------------------------------------------------------------------
# Core logic: sliding-window search for the cleanest slot
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    run_now_start: datetime
    run_now_avg: float
    best_start: datetime
    best_avg: float
    hours: float
    kw: float
    kwh: float
    kg_now: float
    kg_best: float
    kg_saved: float
    pct_saved: float
    method: str


def find_best_window(points: list, now: datetime, hours: float,
                     deadline_h: float, kw: float, method: str) -> Plan:
    pts = sorted(points, key=lambda p: p.start)
    if len(pts) < 2:
        raise ValueError("Need at least 2 forecast points.")
    step_h = (pts[1].start - pts[0].start).total_seconds() / 3600
    n = max(1, round(hours / step_h))                # intervals the job spans

    # The interval containing "now" counts as the run-now slot
    first = 0
    for i, p in enumerate(pts):
        if p.start <= now:
            first = i
    pts = pts[first:]
    if len(pts) < n:
        raise ValueError("Forecast shorter than the job. Reduce --hours.")

    latest_start = now + timedelta(hours=deadline_h - hours)
    windows = []
    for i in range(len(pts) - n + 1):
        if i > 0 and pts[i].start > latest_start:
            break
        avg = sum(p.gco2 for p in pts[i:i + n]) / n
        windows.append((pts[i].start, avg))
    if not windows:
        raise ValueError("Deadline is too tight for the job length.")

    now_start, now_avg = windows[0]
    best_start, best_avg = min(windows, key=lambda w: w[1])
    kwh = kw * hours
    kg_now = now_avg * kwh / 1000
    kg_best = best_avg * kwh / 1000
    saved = kg_now - kg_best
    return Plan(now_start, now_avg, best_start, best_avg, hours, kw, kwh,
                kg_now, kg_best, saved, (saved / kg_now * 100) if kg_now else 0.0,
                method)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_at TEXT, job TEXT, source TEXT, method TEXT,
        hours REAL, kw REAL, kwh REAL,
        now_gco2 REAL, best_gco2 REAL, best_start TEXT,
        kg_now REAL, kg_best REAL, kg_saved REAL,
        action TEXT, exit_code INTEGER)""")
    return con


def log_decision(job, source, plan: Plan, action, exit_code=None):
    with db() as con:
        con.execute("""INSERT INTO decisions (logged_at, job, source, method, hours, kw, kwh,
            now_gco2, best_gco2, best_start, kg_now, kg_best, kg_saved, action, exit_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(UTC).isoformat(), job, source, plan.method, plan.hours, plan.kw,
             plan.kwh, round(plan.run_now_avg, 1), round(plan.best_avg, 1),
             plan.best_start.isoformat(), round(plan.kg_now, 4), round(plan.kg_best, 4),
             round(plan.kg_saved, 4), action, exit_code))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def local(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %d %b %H:%M")


def print_plan(plan: Plan, label: str):
    wait_h = (plan.best_start - plan.run_now_start).total_seconds() / 3600
    print(f"\n  Job          : {label}  ({plan.hours:g} h at {plan.kw:g} kW = {plan.kwh:g} kWh)")
    print(f"  Data         : {plan.method}")
    print(f"  Run now      : {plan.run_now_avg:6.0f} gCO2/kWh  ->  {plan.kg_now:.3f} kg CO2")
    print(f"  Best window  : {plan.best_avg:6.0f} gCO2/kWh  ->  {plan.kg_best:.3f} kg CO2")
    print(f"  Best start   : {local(plan.best_start)}  (in {wait_h:.1f} h)")
    if plan.kg_saved > 0.0005:
        print(f"  Saving       : {plan.kg_saved:.3f} kg CO2  ({plan.pct_saved:.0f}% less)\n")
    else:
        print("  Saving       : none - now is already the cleanest slot\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_plan(args):
    now = datetime.now(UTC)
    pts, method = get_forecast(args, now)
    plan = find_best_window(pts, now, args.hours, args.deadline, args.kw, method)
    print_plan(plan, args.job)
    log_decision(args.job, args.source, plan, "planned")


def cmd_run(args):
    if not args.command:
        sys.exit("Give the command to run after `--`, e.g. -- python train.py")
    now = datetime.now(UTC)
    pts, method = get_forecast(args, now)
    plan = find_best_window(pts, now, args.hours, args.deadline, args.kw, method)
    print_plan(plan, args.job)

    wait_s = (plan.best_start - datetime.now(UTC)).total_seconds()
    if wait_s > 60:
        print(f"  Waiting until {local(plan.best_start)} ... (Ctrl+C to cancel)")
        try:
            while (plan.best_start - datetime.now(UTC)).total_seconds() > 0:
                time.sleep(min(300, max(1, (plan.best_start - datetime.now(UTC)).total_seconds())))
        except KeyboardInterrupt:
            log_decision(args.job, args.source, plan, "cancelled")
            sys.exit("\n  Cancelled.")
    print(f"  Launching: {' '.join(args.command)}\n")
    code = subprocess.call(args.command)
    log_decision(args.job, args.source, plan, "executed", code)
    print(f"\n  Finished with exit code {code}. Logged {plan.kg_saved:.3f} kg CO2 saved.")


def cmd_report(args):
    with db() as con:
        rows = con.execute("""SELECT logged_at, job, source, kwh, now_gco2, best_gco2,
                              kg_saved, action FROM decisions ORDER BY id DESC LIMIT ?""",
                           (args.limit,)).fetchall()
        tot = con.execute("""SELECT COUNT(*), COALESCE(SUM(kwh),0), COALESCE(SUM(kg_saved),0)
                             FROM decisions WHERE action='executed'""").fetchone()
    if not rows:
        print("No decisions logged yet.")
        return
    print(f"\n  {'When (UTC)':<17} {'Job':<18} {'Src':<4} {'kWh':>6} {'Now':>5} {'Best':>5} {'kg saved':>9}  Action")
    for r in rows:
        print(f"  {r[0][:16]:<17} {r[1][:18]:<18} {r[2]:<4} {r[3]:>6.2f} {r[4]:>5.0f} {r[5]:>5.0f} {r[6]:>9.3f}  {r[7]}")
    print(f"\n  Executed jobs: {tot[0]} | Energy: {tot[1]:.1f} kWh | Total CO2 avoided: {tot[2]:.3f} kg\n")


def main():
    ap = argparse.ArgumentParser(description="Carbon-aware job scheduler (real grid data).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--source", choices=["uk", "em"], default="uk",
                       help="uk = GB grid (no key); em = Electricity Maps (token)")
        p.add_argument("--zone", default="IN-WE", help="Electricity Maps zone, e.g. IN-WE, IN-NO")
        p.add_argument("--hours", type=float, default=2, help="Job duration in hours")
        p.add_argument("--kw", type=float, default=1.0, help="Average power draw in kW")
        p.add_argument("--deadline", type=float, default=24, help="Job must finish within N hours")
        p.add_argument("--job", default="batch-job", help="Name for the log")
        p.add_argument("--fixture", help="Offline JSON forecast file (testing)")

    p_plan = sub.add_parser("plan", help="Show the cleanest window and the saving")
    common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="Wait for the cleanest window, then run a command")
    common(p_run)
    p_run.add_argument("command", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run)

    p_rep = sub.add_parser("report", help="Show logged decisions and total CO2 avoided")
    p_rep.add_argument("--limit", type=int, default=20)
    p_rep.set_defaults(func=cmd_report)

    args = ap.parse_args()
    if getattr(args, "fixture", None):
        args.source = "file"
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        args.func(args)
    except urllib.error.URLError as e:
        sys.exit(f"Network error reaching the grid API: {e}")
    except ValueError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()

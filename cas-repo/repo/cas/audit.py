"""
Audit trail. Every run of CAS is recorded twice:

  cas_audit.db   SQLite, structured, queryable (tables below)
  cas_events.jsonl  one JSON event per line, human-readable, append-only

Tables
  runs           one row per CLI invocation: id, time, version, command, full arguments
  forecasts      every forecast fetched: source, method, stats, and the raw API payload
  candidates     every schedule option the optimiser scored (all windows / slots)
  decisions      the chosen schedule, baseline, savings in kg CO2 and Rs, and the reason
  executions     real job launches: scheduled vs actual start, end, exit code, duration
  verifications  after-the-fact check of forecast vs measured carbon intensity

Nothing is ever updated in place except an execution's end time; history is kept.
"""

import json
import os
import platform
import sqlite3
import uuid
from datetime import datetime

from . import VERSION
from .models import UTC

DB_PATH = os.environ.get("CAS_DB", "cas_audit.db")
EVENTS_PATH = os.environ.get("CAS_EVENTS", "cas_events.jsonl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, created_at TEXT, version TEXT, command TEXT, args_json TEXT, host TEXT);
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, zone TEXT, source TEXT, method TEXT,
  fetched_at TEXT, n_points INTEGER, step_h REAL, first_slot TEXT, last_slot TEXT,
  min_gco2 REAL, max_gco2 REAL, mean_gco2 REAL, raw_json TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, zone TEXT, start TEXT,
  avg_gco2 REAL, kg REAL, rs REAL, score REAL);
CREATE TABLE IF NOT EXISTS decisions (
  run_id TEXT PRIMARY KEY, job TEXT, objective TEXT, tariff TEXT, hours REAL, kw REAL, kwh REAL,
  deadline_h REAL, chunks INTEGER,
  baseline_zone TEXT, baseline_start TEXT, baseline_gco2 REAL, baseline_kg REAL, baseline_rs REAL,
  zone TEXT, start TEXT, blocks_json TEXT, kg REAL, rs REAL,
  kg_saved REAL, pct_saved REAL, rs_saved REAL, reason TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, command TEXT, block_no INTEGER,
  scheduled_start TEXT, actual_start TEXT, ended_at TEXT, exit_code INTEGER,
  duration_s REAL, status TEXT);
CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, verified_at TEXT,
  forecast_gco2 REAL, actual_gco2 REAL, actual_kg REAL, error_pct REAL);
"""


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


class Audit:
    def __init__(self, db_path=None, events_path=None):
        self.db_path = db_path or DB_PATH
        self.events_path = events_path or EVENTS_PATH
        self.con = sqlite3.connect(self.db_path)
        self.con.executescript(SCHEMA)
        self.run_id = None

    # ---- generic event stream ---------------------------------------------------
    def event(self, name, **details):
        rec = {"ts": now_iso(), "run_id": self.run_id, "event": name, **details}
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # ---- structured records -----------------------------------------------------
    def start_run(self, command, args: dict):
        self.run_id = uuid.uuid4().hex[:12]
        with self.con:
            self.con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                             (self.run_id, now_iso(), VERSION, command,
                              json.dumps(args, default=str), platform.node()))
        self.event("run_started", command=command, args=args, version=VERSION)
        return self.run_id

    def forecast(self, fc):
        g = [p.gco2 for p in fc.points] or [0]
        with self.con:
            self.con.execute("""INSERT INTO forecasts (run_id, zone, source, method, fetched_at, n_points,
                step_h, first_slot, last_slot, min_gco2, max_gco2, mean_gco2, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.run_id, fc.zone, fc.source, fc.method, fc.fetched_at.isoformat(), len(fc.points),
                 fc.step_h, fc.points[0].start.isoformat() if fc.points else None,
                 fc.points[-1].start.isoformat() if fc.points else None,
                 min(g), max(g), round(sum(g) / len(g), 1), json.dumps(fc.raw, default=str)))
        self.event("forecast_fetched", zone=fc.zone, source=fc.source, method=fc.method,
                   points=len(fc.points), min_gco2=min(g), max_gco2=max(g))

    def decision(self, plan, job, tariff_name, deadline_h, chunks, action):
        with self.con:
            self.con.executemany(
                "INSERT INTO candidates (run_id, zone, start, avg_gco2, kg, rs, score) VALUES (?,?,?,?,?,?,?)",
                [(self.run_id, c["zone"], c["start"], c["avg_gco2"], c["kg"], c["rs"], c["score"])
                 for c in plan.candidates])
            blocks = [{"zone": b.zone, "start": b.start.isoformat(), "end": b.end.isoformat(),
                       "avg_gco2": b.avg_gco2} for b in plan.blocks]
            self.con.execute("""INSERT INTO decisions VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.run_id, job, plan.objective, tariff_name, plan.hours, plan.kw, plan.kwh,
                 deadline_h, chunks, plan.baseline_zone, plan.baseline_start.isoformat(),
                 plan.baseline_gco2, round(plan.baseline_kg, 5),
                 None if plan.baseline_rs is None else round(plan.baseline_rs, 3),
                 plan.zone, plan.start.isoformat(), json.dumps(blocks), round(plan.kg, 5),
                 None if plan.rs is None else round(plan.rs, 3), round(plan.kg_saved, 5),
                 round(plan.pct_saved, 2), None if plan.rs_saved is None else round(plan.rs_saved, 3),
                 plan.reason, action))
        self.event("candidates_scored", count=len(plan.candidates))
        self.event("decision_made", job=job, objective=plan.objective, blocks=blocks,
                   kg_saved=round(plan.kg_saved, 4), pct_saved=round(plan.pct_saved, 1),
                   rs_saved=None if plan.rs_saved is None else round(plan.rs_saved, 2),
                   reason=plan.reason, action=action)

    def set_action(self, action):
        with self.con:
            self.con.execute("UPDATE decisions SET action=? WHERE run_id=?", (action, self.run_id))
        self.event("action_updated", action=action)

    def exec_start(self, command, block_no, scheduled):
        with self.con:
            cur = self.con.execute("""INSERT INTO executions (run_id, command, block_no, scheduled_start,
                actual_start, status) VALUES (?,?,?,?,?,?)""",
                (self.run_id, " ".join(command), block_no, scheduled.isoformat(), now_iso(), "running"))
        self.event("job_started", block=block_no, command=command, scheduled_start=scheduled)
        return cur.lastrowid

    def exec_end(self, row_id, exit_code, duration_s, status):
        with self.con:
            self.con.execute("UPDATE executions SET ended_at=?, exit_code=?, duration_s=?, status=? WHERE id=?",
                             (now_iso(), exit_code, round(duration_s, 2), status, row_id))
        self.event("job_finished", exit_code=exit_code, duration_s=round(duration_s, 2), status=status)

    def verification(self, run_id, forecast_g, actual_g, actual_kg):
        err = (forecast_g - actual_g) / actual_g * 100 if actual_g else None
        with self.con:
            self.con.execute("""INSERT INTO verifications (run_id, verified_at, forecast_gco2, actual_gco2,
                actual_kg, error_pct) VALUES (?,?,?,?,?,?)""",
                (run_id, now_iso(), forecast_g, actual_g, actual_kg, None if err is None else round(err, 2)))
        self.event("verified", target_run=run_id, forecast_gco2=forecast_g, actual_gco2=actual_g, error_pct=err)

    def error(self, message):
        self.event("error", message=message)

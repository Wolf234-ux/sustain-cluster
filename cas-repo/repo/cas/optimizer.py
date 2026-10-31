"""
Scheduling optimiser.

Given one or more zone forecasts, a job (hours, kW), a deadline and optionally
a tariff, choose when (and where) to run so that carbon, cost, or a blend of
both is minimised.

Two job types:
  contiguous  the job runs in one go. Brute-force sliding window over every
              allowed start time: O(N) windows, exact optimum.
  chunked     the job can checkpoint and resume in K equal blocks (e.g. ML
              training with checkpoints). Dynamic programming picks the K
              non-overlapping blocks with the lowest total score: O(K*N), exact.

Savings are always measured against the honest baseline: run now, in the home
(first) zone, in one block.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from .models import Forecast, Point
from .tariffs import Tariff


@dataclass
class Block:
    zone: str
    start: datetime
    end: datetime
    avg_gco2: float


@dataclass
class Plan:
    objective: str
    hours: float
    kw: float
    kwh: float
    baseline_zone: str
    baseline_start: datetime
    baseline_gco2: float
    baseline_kg: float
    baseline_rs: Optional[float]
    blocks: List[Block]
    kg: float
    rs: Optional[float]
    candidates: list = field(default_factory=list)  # every option considered, for the audit log
    reason: str = ""

    @property
    def kg_saved(self): return self.baseline_kg - self.kg

    @property
    def pct_saved(self): return (self.kg_saved / self.baseline_kg * 100) if self.baseline_kg else 0.0

    @property
    def rs_saved(self):
        return None if self.rs is None or self.baseline_rs is None else self.baseline_rs - self.rs

    @property
    def zone(self): return self.blocks[0].zone

    @property
    def start(self): return self.blocks[0].start


def _future_slots(fc: Forecast, now: datetime) -> List[Point]:
    pts = sorted(fc.points, key=lambda p: p.start)
    first = 0
    for i, p in enumerate(pts):
        if p.start <= now:
            first = i
    return pts[first:]


def _slot_kg(p, kw, step): return p.gco2 * kw * step / 1000


def _slot_rs(p, kw, step, tariff): return tariff.rate(p.start) * kw * step if tariff else None


def optimise(forecasts: List[Forecast], now: datetime, hours: float, kw: float, deadline_h: float,
             tariff: Optional[Tariff] = None, objective: str = "carbon", weight: float = 0.5,
             chunks: int = 1) -> Plan:
    if objective not in ("carbon", "cost", "blend"):
        raise ValueError("objective must be carbon, cost or blend")
    if objective in ("cost", "blend") and tariff is None:
        raise ValueError(f"objective '{objective}' needs a --tariff")
    if hours <= 0 or kw <= 0:
        raise ValueError("--hours and --kw must be positive")
    if deadline_h < hours:
        raise ValueError("Deadline is shorter than the job.")

    # ---- honest baseline: run now, home zone, one block --------------------------
    home = forecasts[0]
    hs = _future_slots(home, now)
    step = home.step_h
    n = max(1, round(hours / step))
    if len(hs) < n:
        raise ValueError("Forecast is shorter than the job. Reduce --hours.")
    base = hs[:n]
    b_kg = sum(_slot_kg(p, kw, step) for p in base)
    b_rs = sum(_slot_rs(p, kw, step, tariff) for p in base) if tariff else None
    b_g = sum(p.gco2 for p in base) / n

    def score(kg, rs):
        if objective == "carbon":
            return kg
        if objective == "cost":
            return rs
        return weight * kg / b_kg + (1 - weight) * rs / b_rs

    best = None
    candidates = []
    for fc in forecasts:
        slots = _future_slots(fc, now)
        st = fc.step_h
        m = max(1, round(hours / st))
        zt = tariff if (tariff and (fc.zone.startswith("IN") or fc.zone == home.zone)) else None
        if objective != "carbon" and zt is None:
            continue  # cannot price this zone
        deadline = now + timedelta(hours=deadline_h)

        if chunks <= 1:
            latest = now + timedelta(hours=deadline_h - hours)
            for i in range(len(slots) - m + 1):
                if i > 0 and slots[i].start > latest:
                    break
                w = slots[i:i + m]
                kg = sum(_slot_kg(p, kw, st) for p in w)
                rs = sum(_slot_rs(p, kw, st, zt) for p in w) if zt else None
                s = score(kg, rs)
                cand = {"zone": fc.zone, "start": w[0].start.isoformat(), "avg_gco2": round(kg * 1000 / (kw * st * m), 1),
                        "kg": round(kg, 5), "rs": None if rs is None else round(rs, 3), "score": round(s, 6)}
                candidates.append(cand)
                if best is None or s < best[0] - 1e-12:
                    best = (s, fc, [(i, m)], kg, rs, st)
        else:
            if m % chunks:
                raise ValueError(f"Job of {m} slots cannot split into {chunks} equal blocks. Adjust --chunks.")
            L = m // chunks
            usable = [p for p in slots if p.start + timedelta(hours=st) <= deadline + timedelta(seconds=1)]
            N = len(usable)
            if N < m:
                raise ValueError("Not enough slots before the deadline for this job.")
            slot_s = [score(_slot_kg(p, kw, st), _slot_rs(p, kw, st, zt) if zt else None) for p in usable]
            for i, p in enumerate(usable):
                candidates.append({"zone": fc.zone, "start": p.start.isoformat(), "avg_gco2": p.gco2,
                                   "kg": round(_slot_kg(p, kw, st), 5),
                                   "rs": None if not zt else round(_slot_rs(p, kw, st, zt), 3),
                                   "score": round(slot_s[i], 6)})
            pre = [0.0]
            for v in slot_s:
                pre.append(pre[-1] + v)
            INF = float("inf")
            # dp[k][i]: best score placing k blocks inside the first i slots
            dp = [[INF] * (N + 1) for _ in range(chunks + 1)]
            take = [[False] * (N + 1) for _ in range(chunks + 1)]
            for i in range(N + 1):
                dp[0][i] = 0.0
            for k in range(1, chunks + 1):
                for i in range(1, N + 1):
                    dp[k][i] = dp[k][i - 1]
                    if i >= L and dp[k - 1][i - L] + (pre[i] - pre[i - L]) < dp[k][i] - 1e-12:
                        dp[k][i] = dp[k - 1][i - L] + (pre[i] - pre[i - L])
                        take[k][i] = True
            if dp[chunks][N] == INF:
                continue
            blocks, k, i = [], chunks, N
            while k > 0:
                if take[k][i]:
                    blocks.append((i - L, L)); i -= L; k -= 1
                else:
                    i -= 1
            blocks.reverse()
            kg = sum(_slot_kg(usable[j], kw, st) for a, l in blocks for j in range(a, a + l))
            rs = sum(_slot_rs(usable[j], kw, st, zt) for a, l in blocks for j in range(a, a + l)) if zt else None
            s = dp[chunks][N]
            if best is None or s < best[0] - 1e-12:
                best = (s, fc, blocks, kg, rs, st, usable)

    if best is None:
        raise ValueError("No feasible schedule found.")

    fc, idx_blocks, kg, rs, st = best[1], best[2], best[3], best[4], best[5]
    slots = best[6] if len(best) > 6 else _future_slots(fc, now)
    blocks = []
    for a, l in idx_blocks:
        w = slots[a:a + l]
        blocks.append(Block(fc.zone, w[0].start, w[-1].start + timedelta(hours=st),
                            round(sum(p.gco2 for p in w) / l, 1)))

    plan = Plan(objective, hours, kw, kw * hours, home.zone, base[0].start, round(b_g, 1),
                b_kg, b_rs, blocks, kg, rs, candidates)
    plan.reason = explain(plan, now)
    return plan


def explain(p: Plan, now: datetime) -> str:
    wait = max(0.0, (p.start - now).total_seconds() / 3600)
    if p.kg_saved <= 1e-6 and (p.rs_saved is None or p.rs_saved <= 1e-6):
        return "Running now is already the best option before the deadline."
    parts = []
    if len(p.blocks) > 1:
        parts.append(f"Split into {len(p.blocks)} blocks")
    if p.zone != p.baseline_zone:
        parts.append(f"move to zone {p.zone}")
    parts.append(f"start in {wait:.1f} h")
    msg = ", ".join(parts)
    msg = msg[0].upper() + msg[1:]
    if p.kg_saved >= 0:
        msg += f": {p.kg_saved:.3f} kg CO2 less ({p.pct_saved:.0f}%)"
    else:
        msg += f": {-p.kg_saved:.3f} kg CO2 more ({-p.pct_saved:.0f}%, cost objective)"
    if p.rs_saved is not None:
        msg += f", Rs {p.rs_saved:.2f} {'saved' if p.rs_saved >= 0 else 'extra'}"
    return msg + "."

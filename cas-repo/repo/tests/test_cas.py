"""Run with:  python -m unittest discover -s tests -v   (from the project folder)"""

import itertools
import json
import os
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cas.audit import Audit                      # noqa: E402
from cas.india import CEA_MEAN_GCO2, hourly_profile, india_model_forecast  # noqa: E402
from cas.models import IST, UTC, Forecast, Point, parse_ts  # noqa: E402
from cas.optimizer import optimise               # noqa: E402
from cas.tariffs import get_tariff               # noqa: E402

T0 = datetime(2026, 10, 31, 12, 30, tzinfo=UTC)  # 18:00 IST


def fc(values, zone="GB", step_min=30, start=T0):
    return Forecast(zone, "test", "test", [Point(start + timedelta(minutes=step_min * i), v)
                                            for i, v in enumerate(values)])


class TestOptimiser(unittest.TestCase):
    def test_contiguous_matches_brute_force(self):
        rnd = random.Random(1)
        for _ in range(200):
            vals = [rnd.uniform(50, 900) for _ in range(40)]
            hours, deadline = rnd.choice([0.5, 1, 2, 3]), rnd.choice([4, 8, 12, 19])
            p = optimise([fc(vals)], T0, hours, 2.0, deadline)
            n = int(hours * 2)
            last_start = int((deadline - hours) * 2)
            best = min(sum(vals[i:i + n]) / n for i in range(0, last_start + 1))
            self.assertAlmostEqual(p.kg * 1000 / p.kwh, best, places=6)

    def test_chunked_matches_brute_force(self):
        rnd = random.Random(2)
        for _ in range(60):
            vals = [rnd.uniform(50, 900) for _ in range(14)]
            p = optimise([fc(vals)], T0, 2, 1.0, 7, chunks=2)   # 4 slots, 2 blocks of 2
            best = float("inf")
            for a, b in itertools.combinations(range(13), 2):
                if b >= a + 2:
                    best = min(best, vals[a] + vals[a + 1] + vals[b] + vals[b + 1])
            self.assertAlmostEqual(p.kg * 1000 / (1.0 * 0.5), best, places=6)

    def test_chunked_never_worse_than_contiguous(self):
        rnd = random.Random(3)
        for _ in range(50):
            vals = [rnd.uniform(50, 900) for _ in range(48)]
            one = optimise([fc(vals)], T0, 4, 1, 24)
            two = optimise([fc(vals)], T0, 4, 1, 24, chunks=2)
            self.assertLessEqual(two.kg, one.kg + 1e-9)

    def test_run_now_when_now_is_cleanest(self):
        p = optimise([fc([100] + [500] * 20)], T0, 0.5, 1, 6)
        self.assertEqual(p.start, T0)
        self.assertAlmostEqual(p.kg_saved, 0)

    def test_deadline_respected(self):
        vals = [500] * 10 + [10] * 10          # clean slots only after 5 h
        p = optimise([fc(vals)], T0, 1, 1, 4)
        self.assertLessEqual(p.blocks[-1].end, T0 + timedelta(hours=4))

    def test_spatial_picks_cleaner_zone(self):
        p = optimise([fc([800] * 20, "IN"), fc([150] * 20, "GB")], T0, 2, 1, 8)
        self.assertEqual(p.zone, "GB")
        self.assertEqual(p.baseline_zone, "IN")

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            optimise([fc([1] * 10)], T0, 3, 1, 2)            # deadline < job
        with self.assertRaises(ValueError):
            optimise([fc([1] * 10)], T0, 1, 1, 4, objective="cost")  # no tariff


class TestIndiaModel(unittest.TestCase):
    def test_mean_matches_cea(self):
        self.assertAlmostEqual(sum(hourly_profile()) / 24, CEA_MEAN_GCO2, delta=0.5)

    def test_shape(self):
        prof = hourly_profile()
        self.assertTrue(11 <= min(range(24), key=lambda h: prof[h]) <= 14)   # solar dip
        self.assertTrue(18 <= max(range(24), key=lambda h: prof[h]) <= 21)   # evening peak

    def test_forecast_slots(self):
        f = india_model_forecast(T0)
        self.assertEqual(len(f.points), 96)
        self.assertEqual(f.step_h, 0.5)


class TestTariffs(unittest.TestCase):
    def test_gujarat_htp1(self):
        t = get_tariff("gj-htp1")
        at = lambda h: t.rate(datetime(2026, 11, 1, h, tzinfo=IST))
        self.assertAlmostEqual(at(3), 4.20)    # normal
        self.assertAlmostEqual(at(8), 5.05)    # morning peak +0.85
        self.assertAlmostEqual(at(13), 3.60)   # solar discount -0.60
        self.assertAlmostEqual(at(19), 5.05)   # evening peak
        self.assertAlmostEqual(at(17), 4.20)   # discount ends at 17:00

    def test_flat_and_cost_objective(self):
        t = get_tariff("gj-htp1")
        f = fc([700] * 48, "IN")              # flat carbon -> cost decides
        p = optimise([f], T0, 2, 10, 24, tariff=t, objective="cost")
        self.assertEqual(p.start.astimezone(IST).hour, 11)
        self.assertGreater(p.rs_saved, 0)

    def test_parse_ts(self):
        self.assertEqual(parse_ts("2026-10-31T05:00Z"), datetime(2026, 10, 31, 5, tzinfo=UTC))
        self.assertEqual(parse_ts("2026-10-31T18:00+05:30"), datetime(2026, 10, 31, 12, 30, tzinfo=UTC))


class TestAudit(unittest.TestCase):
    def test_everything_logged(self):
        d = tempfile.mkdtemp()
        a = Audit(os.path.join(d, "a.db"), os.path.join(d, "e.jsonl"))
        a.start_run("plan", {"hours": 2})
        f = fc([300, 200, 100, 400, 500, 600, 700, 800])
        a.forecast(f)
        p = optimise([f], T0, 1, 1, 4)
        a.decision(p, "job", None, 4, 1, "planned")
        con = sqlite3.connect(os.path.join(d, "a.db"))
        for table, expected in (("runs", 1), ("forecasts", 1), ("decisions", 1)):
            self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], expected)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], len(p.candidates))
        with open(os.path.join(d, "e.jsonl")) as fh:
            events = [json.loads(line)["event"] for line in fh]
        self.assertEqual(events, ["run_started", "forecast_fetched", "candidates_scored", "decision_made"])


if __name__ == "__main__":
    unittest.main()

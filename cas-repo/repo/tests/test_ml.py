"""ML tests. Skipped when scikit-learn or the public data repo is missing.

  CAS_DATA_REPO=/path/to/sustain-cluster python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import numpy as np
    import pandas as pd
    from cas import ml
    HAVE_ML = True
except ImportError:  # pragma: no cover
    HAVE_ML = False

REPO = os.environ.get("CAS_DATA_REPO", "")
HAVE_DATA = os.path.isdir(os.path.join(REPO, "data", "carbon_intensity", "IN-WE"))


@unittest.skipUnless(HAVE_ML, "numpy/pandas/scikit-learn not installed")
class TestFeatures(unittest.TestCase):
    def test_no_future_leakage(self):
        # a synthetic series where every value equals its own index: any feature equal to
        # the target (or later) would reveal leakage
        idx = pd.date_range("2024-01-01", periods=400, freq="h", tz="UTC")
        y = pd.Series(np.arange(400, dtype=float), index=idx)
        w = pd.DataFrame({"temp": 25.0, "rh": 50.0, "cloud": 10.0, "wind": 5.0}, index=idx)
        X, t, t_times, o_times = ml.make_rows(y, w, idx[200:201])
        self.assertEqual(len(X), ml.H)
        for col in ("y0", "y_same", "y_week", "m24"):
            self.assertTrue((X[col].to_numpy() <= 200).all(), col)      # nothing after the origin
        self.assertTrue((t == 200 + X["h"].to_numpy()).all())          # target is origin + h

    def test_same_hour_lag(self):
        idx = pd.date_range("2024-01-01", periods=400, freq="h", tz="UTC")
        y = pd.Series(np.arange(400, dtype=float), index=idx)
        w = pd.DataFrame({"temp": 0.0, "rh": 0.0, "cloud": 0.0, "wind": 0.0}, index=idx)
        X, t, _, _ = ml.make_rows(y, w, idx[200:201])
        # for h=1..24 the latest same hour is target-24; for 25..48 it is target-48
        exp = np.where(X["h"] <= 24, t - 24, t - 48)
        self.assertTrue((X["y_same"].to_numpy() == exp).all())


@unittest.skipUnless(HAVE_ML and HAVE_DATA, "set CAS_DATA_REPO to a clone of HewlettPackard/sustain-cluster")
class TestOnRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = ml.load_weather(REPO)
        cls.price = ml.load_price(REPO)
        cls.fc = ml.Forecaster("rs_kwh", "Rs/kWh").fit(cls.price, cls.w, "2023-01-08", "2023-06-30", every_h=6)

    def test_data_loads(self):
        c = ml.load_carbon(REPO)
        self.assertGreater(len(c), 30000)
        self.assertTrue(500 < c.mean() < 900)
        self.assertGreater(len(self.price), 40000)

    def test_forecast_shape_and_no_peeking(self):
        origin = pd.Timestamp("2023-09-01 12:30", tz="UTC")
        f = self.fc.forecast(self.price, self.w, origin)
        self.assertEqual(len(f), ml.H)
        self.assertTrue((f.index > origin).all())
        self.assertFalse(np.isnan(f.to_numpy()).any())

    def test_beats_profile_out_of_sample(self):
        y = self.price
        origins = y["2023-07-01":"2023-08-31"].index[::12]
        X, t, t_times, _ = ml.make_rows(y, self.w, origins)
        ok = ~np.isnan(t) & ~np.isnan(X["y_same"].to_numpy())
        p = self.fc.predict_rows(X[ok])
        tr = y["2023-01-08":"2023-06-30"]
        prof = tr.groupby(tr.index.tz_convert(ml.IST).hour).mean()
        pp = prof.reindex(t_times[ok].tz_convert(ml.IST).hour).to_numpy()
        self.assertLess(np.mean(np.abs(p - t[ok])), np.mean(np.abs(pp - t[ok])))


if __name__ == "__main__":
    unittest.main()

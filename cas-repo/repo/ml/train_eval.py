"""
Train the CAS forecasters and test them honestly on a year the model never saw.

  python ml/train_eval.py --repo /path/to/sustain-cluster

Train: up to 31 Dec 2023.  Test: every day of 2024 (held out).
Baselines it must beat:
  persistence  tomorrow's hour = the latest same hour already seen
  profile      average for that hour and month in the training years
  static       the CAS v2 hand-built India curve (carbon only)
Decision test: for every test day and four submit times, schedule a 4 h job
with a 24 h deadline using each forecast, then score the choice against what
the grid ACTUALLY did. "Captured" = share of the best possible saving achieved.
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cas import ml                        # noqa: E402
from cas.india import hourly_profile      # noqa: E402

JOB_H, DEADLINE_H, SUBMITS = 4, 24, (0, 6, 12, 18)  # IST submit hours


def metrics(pred, true):
    ok = ~np.isnan(pred) & ~np.isnan(true)
    e = pred[ok] - true[ok]
    return {"mae": float(np.mean(np.abs(e))), "mape_pct": float(np.mean(np.abs(e) / np.abs(true[ok])) * 100),
            "n": int(ok.sum())}


def schedule_eval(y, preds_by_name, test_days):
    """preds_by_name: name -> function(origin) -> Series of 48 forecasts."""
    res = {n: [] for n in preds_by_name}
    oracle, nows = [], []
    for day in test_days:
        for sh in SUBMITS:
            origin = (pd.Timestamp(day).tz_localize(ml.IST) + pd.Timedelta(hours=sh)).tz_convert("UTC")
            actual = y[origin + pd.Timedelta(seconds=1):].iloc[:ml.H]
            if len(actual) < ml.H or actual.isna().any():
                continue
            a = actual.to_numpy()
            starts = range(0, DEADLINE_H - JOB_H + 1)
            win = np.array([a[s:s + JOB_H].mean() for s in starts])
            now, best = win[0], win.min()
            nows.append(now); oracle.append(best)
            for n, f in preds_by_name.items():
                p = f(origin).to_numpy()
                pw = np.array([p[s:s + JOB_H].mean() for s in starts])
                res[n].append(win[int(np.argmin(pw))])
    nows, oracle = np.array(nows), np.array(oracle)
    out = {"cases": int(len(nows)),
           "oracle_saving_pct": float(np.mean((nows - oracle) / nows) * 100)}
    for n, r in res.items():
        r = np.array(r)
        out[n] = {"realised_saving_pct": float(np.mean((nows - r) / nows) * 100),
                  "captured_pct": float((nows - r).sum() / max(1e-9, (nows - oracle).sum()) * 100)}
    return out


def run(name, y, weather, train_start, train_end, test_year, unit, static=None, residual=True):
    t0 = time.time()
    fc = ml.Forecaster(name, unit, residual=residual).fit(y, weather, train_start, train_end)
    secs = time.time() - t0
    # ---- accuracy on held-out year (origins every 6 h) ----
    test = y[f"{test_year}-01-01": f"{test_year}-12-31"]
    origins = test.index[::6]
    X, tgt, t_times, o_times = ml.make_rows(y, weather, origins)
    keep = ~np.isnan(tgt)
    X, tgt, t_times = X[keep], tgt[keep], t_times[keep]
    p_ml = fc.predict_rows(X)
    p_pers = X["y_same"].to_numpy()
    tr = y[train_start:train_end]
    loc = tr.index.tz_convert(ml.IST)
    prof = tr.groupby([loc.month, loc.hour]).mean()
    tl = t_times.tz_convert(ml.IST)
    p_prof = prof.reindex(list(zip(tl.month, tl.hour))).to_numpy()
    acc = {"ml": metrics(p_ml, tgt), "persistence": metrics(p_pers, tgt), "profile": metrics(p_prof, tgt)}
    if static is not None:
        acc["cas_v2_static"] = metrics(np.array([static[h] for h in tl.hour]), tgt)
    # by horizon
    hb = {}
    for lo, hi in ((1, 6), (7, 24), (25, 48)):
        m = (X["h"] >= lo) & (X["h"] <= hi)
        hb[f"h{lo}-{hi}"] = {"ml_mape": metrics(p_ml[m.to_numpy()], tgt[m.to_numpy()])["mape_pct"],
                             "persistence_mape": metrics(p_pers[m.to_numpy()], tgt[m.to_numpy()])["mape_pct"]}
    # ---- decisions ----
    days = pd.date_range(f"{test_year}-01-08", f"{test_year}-12-29", freq="D")
    def f_ml(o): return fc.forecast(y, weather, o)
    def f_pers(o):
        last = y[:o].dropna().index[-1]
        idx = pd.date_range(last + pd.Timedelta(hours=1), periods=ml.H, freq="h")
        lag = [t - pd.Timedelta(hours=24 * int(np.ceil(((t - last) / pd.Timedelta(hours=1)) / 24))) for t in idx]
        return pd.Series(y.reindex(lag).to_numpy(), index=idx)
    def f_prof(o):
        last = y[:o].dropna().index[-1]
        idx = pd.date_range(last + pd.Timedelta(hours=1), periods=ml.H, freq="h").tz_convert(ml.IST)
        return pd.Series(prof.reindex(list(zip(idx.month, idx.hour))).to_numpy(), index=idx)
    preds = {"ml": f_ml, "persistence": f_pers, "profile": f_prof}
    if static is not None:
        def f_static(o):
            last = y[:o].dropna().index[-1]
            idx = pd.date_range(last + pd.Timedelta(hours=1), periods=ml.H, freq="h").tz_convert(ml.IST)
            return pd.Series([static[h] for h in idx.hour], index=idx)
        preds["cas_v2_static"] = f_static
    dec = schedule_eval(y, preds, days)
    imp = dict(zip(ml.FEATURES, np.round(_perm_importance(fc, X.iloc[::20], tgt[::20]), 4)))
    return fc, {"signal": name, "unit": unit, "trained_on": fc.trained_on, "train_seconds": round(secs, 1),
                "test_year": test_year, "accuracy": acc, "by_horizon": hb, "scheduling": dec,
                "feature_importance": imp}


def _perm_importance(fc, X, y, seed=0):
    rng = np.random.default_rng(seed)
    base = np.mean(np.abs(fc.predict_rows(X) - y))
    out = []
    for c in ml.FEATURES:
        Xp = X.copy(); Xp[c] = rng.permutation(Xp[c].to_numpy())
        out.append(np.mean(np.abs(fc.predict_rows(Xp) - y)) - base)
    return np.array(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to a clone of HewlettPackard/sustain-cluster")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "models"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    w = ml.load_weather(a.repo)
    results = {}
    fc_c, results["carbon"] = run("gco2", ml.load_carbon(a.repo), w, "2021-01-08", "2023-12-31", 2024,
                                  "gCO2/kWh", static=ml_static if (ml_static := hourly_profile()) else None)
    ml.save(fc_c, os.path.join(a.out, "in_we_carbon.joblib"))
    fc_p, results["price"] = run("rs_kwh", ml.load_price(a.repo), w, "2020-01-08", "2023-12-31", 2024, "Rs/kWh")
    ml.save(fc_p, os.path.join(a.out, "in_price.joblib"))
    with open(os.path.join(a.out, "results.json"), "w") as f:
        json.dump(results, f, indent=1)
    print(json.dumps(results, indent=1))

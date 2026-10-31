"""
ML forecasting for CAS: predict the next 48 hours of grid carbon intensity or
electricity price, so the scheduler can plan against a learned forecast where
no official forecast exists (India).

Model
  One gradient-boosted tree ensemble (scikit-learn HistGradientBoostingRegressor)
  per signal, trained "direct multi-horizon": each training row is
  (forecast origin t, horizon h in 1..48) -> value at t+h. One model then
  predicts every hour ahead.

Features (all known at the forecast origin)
  calendar   target hour (IST), day of week, day of year (sin/cos), horizon h
  history    value at origin, latest same-hour value, value one week before
             the target hour, mean and spread of the last 24 h
  weather    temperature, humidity, cloud cover, wind at the target hour.
             In backtests these are recorded actuals, standing in for a
             weather forecast; in production, pass a weather forecast.

Data (public, reproducible): HPE SustainCluster repository (MIT licence),
  github.com/HewlettPackard/sustain-cluster
  - data/carbon_intensity/IN-WE/   Electricity Maps hourly, Western India, 2021-2024
  - data/electricity_prices/processed/IN/   India market price, INR/MWh, 2020-2024
  - data/weather/IN-WE/            Open-Meteo hourly, Mumbai, 2020-2024

Needs: numpy, pandas, scikit-learn (pip install -r requirements-ml.txt).
The rest of CAS stays standard-library only.
"""

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

H = 48  # forecast horizon, hours
IST = "Asia/Kolkata"
FEATURES = ["h", "hour", "dow", "doy_s", "doy_c", "y0", "y_same", "y_week",
            "m24", "s24", "temp", "rh", "cloud", "wind"]


# ---------------------------------------------------------------------------
# Loading the public data
# ---------------------------------------------------------------------------
def load_carbon(repo: str, zone: str = "IN-WE") -> pd.Series:
    files = sorted(glob.glob(os.path.join(repo, "data/carbon_intensity", zone, "*", "*.csv")))
    df = pd.concat(pd.read_csv(f) for f in files)
    s = pd.Series(df["Carbon Intensity gCO₂eq/kWh (direct)"].to_numpy(),
                  index=pd.to_datetime(df["Datetime (UTC)"], utc=True), name="gco2")
    return s[~s.index.duplicated()].sort_index().asfreq("h").interpolate(limit=6)


def load_price(repo: str) -> pd.Series:
    files = sorted(glob.glob(os.path.join(repo, "data/electricity_prices/processed/IN", "*", "*.csv")))
    df = pd.concat(pd.read_csv(f) for f in files)
    idx = pd.to_datetime(df["Datetime"]).dt.tz_localize(IST).dt.tz_convert("UTC")
    s = pd.Series(df["Price (INR/MWh)"].to_numpy() / 1000.0, index=idx, name="rs_kwh")  # Rs per kWh
    return s[~s.index.duplicated()].sort_index().asfreq("h").interpolate(limit=6)


def load_weather(repo: str, zone: str = "IN-WE") -> pd.DataFrame:
    parts = []
    for f in sorted(glob.glob(os.path.join(repo, "data/weather", zone, "*.json"))):
        h = json.load(open(f))["hourly"]
        d = pd.DataFrame({"temp": h["temperature_2m"], "rh": h["relative_humidity_2m"],
                          "cloud": h["cloudcover"], "wind": h["windspeed_10m"]},
                         index=pd.to_datetime(h["time"]).tz_localize(IST).tz_convert("UTC"))
        parts.append(d)
    w = pd.concat(parts)
    return w[~w.index.duplicated()].sort_index().asfreq("h").interpolate(limit=6)


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
def make_rows(y: pd.Series, weather: pd.DataFrame, origins: pd.DatetimeIndex, with_target=True):
    """One row per (origin, horizon). Returns X (DataFrame), target (array or None), target times."""
    vals = y.to_numpy()
    pos = pd.Series(np.arange(len(y)), index=y.index)
    m24 = y.rolling(24, min_periods=12).mean()
    s24 = y.rolling(24, min_periods=12).std()
    w = weather.reindex(y.index.union(weather.index)).interpolate(limit=6)
    o = origins[origins.isin(y.index)]
    oi = pos.loc[o].to_numpy()
    hs = np.arange(1, H + 1)
    oi_r = np.repeat(oi, H)
    h_r = np.tile(hs, len(oi))
    ti = oi_r + h_r
    ok = ti < len(vals) if with_target else np.ones_like(ti, dtype=bool)
    oi_r, h_r, ti = oi_r[ok], h_r[ok], ti[ok]
    t_times = y.index[oi_r] + pd.to_timedelta(h_r, unit="h")
    loc = t_times.tz_convert(IST)
    same = ti - 24 * np.ceil(h_r / 24).astype(int)          # latest same hour known at origin
    week = ti - 168
    def at(idx):
        out = np.full(len(idx), np.nan)
        good = (idx >= 0) & (idx < len(vals))
        out[good] = vals[idx[good]]
        return out
    wt = w.reindex(t_times)
    doy = loc.dayofyear.to_numpy()
    X = pd.DataFrame({
        "h": h_r, "hour": loc.hour, "dow": loc.dayofweek,
        "doy_s": np.sin(2 * np.pi * doy / 365.25), "doy_c": np.cos(2 * np.pi * doy / 365.25),
        "y0": vals[oi_r], "y_same": at(same), "y_week": at(week),
        "m24": m24.to_numpy()[oi_r], "s24": s24.to_numpy()[oi_r],
        "temp": wt["temp"].to_numpy(), "rh": wt["rh"].to_numpy(),
        "cloud": wt["cloud"].to_numpy(), "wind": wt["wind"].to_numpy(),
    })
    target = at(ti) if with_target else None
    return X, target, t_times, y.index[oi_r]


# ---------------------------------------------------------------------------
# Train / predict
# ---------------------------------------------------------------------------
@dataclass
class Forecaster:
    signal: str
    unit: str
    model: HistGradientBoostingRegressor = None
    residual: bool = True   # learn the change from the latest same-hour value (robust to level shifts)
    trained_on: str = ""
    metrics: dict = field(default_factory=dict)

    def fit(self, y, weather, start, end, every_h=2, seed=0):
        y_tr = y[:end]
        origins = y_tr[start:end].index[::every_h]
        X, t, _, _ = make_rows(y_tr, weather, origins)
        keep = ~np.isnan(t)
        self.model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, max_leaf_nodes=63,
                                                   min_samples_leaf=40, l2_regularization=1.0,
                                                   random_state=seed)
        base = X["y_same"].to_numpy() if self.residual else 0.0
        keep &= ~np.isnan(X["y_same"].to_numpy())
        self.model.fit(X[keep][FEATURES], (t - base)[keep])
        self.trained_on = f"{start} to {end}, {int(keep.sum()):,} rows"
        return self

    def predict_rows(self, X):
        base = X["y_same"].to_numpy() if self.residual else 0.0
        return self.model.predict(X[FEATURES]) + base

    def forecast(self, y_hist: pd.Series, weather: pd.DataFrame, origin) -> pd.Series:
        """48 hourly predictions after `origin`, using only data up to `origin`."""
        hist = y_hist[:pd.Timestamp(origin).tz_convert("UTC")].dropna()
        origin = hist.index[-1]          # the last reading known at the origin
        future = pd.date_range(origin + pd.Timedelta(hours=1), periods=H, freq="h")
        ext = pd.concat([hist, pd.Series(np.nan, index=future)])
        X, _, t_times, _ = make_rows(ext, weather, pd.DatetimeIndex([origin]), with_target=False)
        return pd.Series(self.predict_rows(X), index=t_times, name=self.signal)


def save(fc: Forecaster, path: str):
    import joblib
    joblib.dump(fc, path)


def load(path: str) -> Forecaster:
    import joblib
    return joblib.load(path)

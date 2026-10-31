"""
Connects the trained ML forecasters to the scheduler.

  --source ml          48 h carbon forecast for Western India from models/in_we_carbon.joblib
  --tariff ml-price    hour-by-hour price forecast (Rs/kWh) from models/in_price.joblib

History and weather come from a local clone of the public HPE SustainCluster
data (set CAS_DATA_REPO, or pass --repo). In that mode CAS forecasts as of
--now inside 2021-2024, which is how the models are back-tested. In a live
deployment the same models take the live grid feed instead.

Imported only when an ML option is used, so the rest of CAS needs no extra packages.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime

from .models import IST, Forecast, Point

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.environ.get("CAS_MODEL_DIR", os.path.join(HERE, "models"))


def _repo(repo=None):
    r = repo or os.environ.get("CAS_DATA_REPO")
    if not r or not os.path.isdir(os.path.join(r, "data")):
        raise RuntimeError("ML options need the public data: clone github.com/HewlettPackard/sustain-cluster "
                           "and pass --repo PATH (or set CAS_DATA_REPO).")
    return r


_cache = {}


def _load(kind, repo):
    from . import ml
    if kind not in _cache:
        path = os.path.join(MODEL_DIR, "in_we_carbon.joblib" if kind == "carbon" else "in_price.joblib")
        if not os.path.exists(path):
            raise RuntimeError(f"No trained model at {path}. Run: python ml/train_eval.py --repo {repo}")
        series = ml.load_carbon(repo) if kind == "carbon" else ml.load_price(repo)
        if "weather" not in _cache:
            _cache["weather"] = ml.load_weather(repo)
        _cache[kind] = (ml.load(path), series, _cache["weather"])
    return _cache[kind]


def ml_forecast(now: datetime, repo=None) -> Forecast:
    repo = _repo(repo)
    model, series, weather = _load("carbon", repo)
    pred = model.forecast(series, weather, now)
    pts = [Point(t.to_pydatetime(), round(float(v), 1)) for t, v in pred.items()]
    return Forecast("IN-WE", "ml", f"ML forecast (gradient boosting, trained {model.trained_on})",
                    pts, local_tz=IST, raw={"model": "HistGradientBoostingRegressor", "trained_on": model.trained_on})


@dataclass
class DynamicTariff:
    name: str
    rates: dict = field(default_factory=dict)   # UTC hour (datetime) -> Rs/kWh
    note: str = ""

    def rate(self, t):
        key = t.replace(minute=0, second=0, microsecond=0)
        if key in self.rates:
            return self.rates[key]
        # nearest forecast hour (price data is stamped on IST hours, i.e. :30 UTC)
        return min(self.rates.items(), key=lambda kv: abs((kv[0] - t).total_seconds()))[1]


def ml_price_tariff(now: datetime, repo=None) -> DynamicTariff:
    repo = _repo(repo)
    model, series, weather = _load("price", repo)
    pred = model.forecast(series, weather, now)
    rates = {t.to_pydatetime().replace(minute=0, second=0): round(float(v), 3) for t, v in pred.items()}
    return DynamicTariff("ML price forecast, India power exchange (Rs/kWh)", rates,
                         note=f"Gradient boosting, trained {model.trained_on}")

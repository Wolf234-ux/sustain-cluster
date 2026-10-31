"""
Grid data sources. Each returns a Forecast of half-hourly or hourly points.

  gb        National Grid ESO Carbon Intensity API - live 48h forecast, free, no key.
  em:ZONE   Electricity Maps (e.g. em:IN-WE). Needs ELECTRICITYMAPS_TOKEN.
            Free keys get latest + 24h history but no forecast, so CAS builds a
            persistence forecast (same hour yesterday). Paid keys use the real forecast.
  india     India model calibrated to CEA + Ember data (see india.py). No key.
  file:PATH Saved forecast JSON: [{"start": iso, "gco2": n}, ...] or a raw GB API reply.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from .india import india_model_forecast
from .models import IST, UTC, Forecast, Point, parse_ts

EM_BASE = "https://api.electricitymaps.com/v3"
GB_BASE = "https://api.carbonintensity.org.uk"


def _get_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "carbon-aware-scheduler/2.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_gb(now: datetime) -> Forecast:
    frm = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    data = _get_json(f"{GB_BASE}/intensity/{frm}/fw48h")
    return Forecast("GB", "gb-api", "GB Carbon Intensity API 48h forecast",
                    _gb_points(data), raw=data)


def _gb_points(data):
    pts = []
    for row in data.get("data", []):
        v = row["intensity"].get("forecast")
        if v is None:
            v = row["intensity"].get("actual")
        if v is not None:
            pts.append(Point(parse_ts(row["from"]), float(v)))
    return pts


def gb_actuals(start: datetime, end: datetime):
    """Measured intensity for a past period (used by `cas verify`)."""
    a = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    b = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    data = _get_json(f"{GB_BASE}/intensity/{a}/{b}")
    vals = [r["intensity"]["actual"] for r in data.get("data", []) if r["intensity"].get("actual") is not None]
    return (sum(vals) / len(vals)) if vals else None


def fetch_em(zone: str, now: datetime) -> Forecast:
    token = os.environ.get("ELECTRICITYMAPS_TOKEN")
    if not token:
        raise RuntimeError("Set ELECTRICITYMAPS_TOKEN (free at app.electricitymaps.com) or use --source india")
    h = {"auth-token": token}
    tz = IST if zone.startswith("IN") else UTC
    try:
        data = _get_json(f"{EM_BASE}/carbon-intensity/forecast?zone={zone}", h)
        pts = [Point(parse_ts(r["datetime"]), float(r["carbonIntensity"]))
               for r in data.get("forecast", []) if r.get("carbonIntensity") is not None]
        if pts:
            return Forecast(zone, "electricitymaps", "Electricity Maps forecast", pts, local_tz=tz, raw=data)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 402, 403, 404):
            raise
    data = _get_json(f"{EM_BASE}/carbon-intensity/history?zone={zone}", h)
    hist = [Point(parse_ts(r["datetime"]), float(r["carbonIntensity"]))
            for r in data.get("history", []) if r.get("carbonIntensity") is not None]
    if not hist:
        raise RuntimeError(f"No data returned for zone {zone}")
    # Persistence forecast: tomorrow at hour H looks like today at hour H. Repeat to cover 48h.
    pts = [Point(p.start + timedelta(hours=24 * k), p.gco2) for k in (1, 2) for p in hist]
    return Forecast(zone, "electricitymaps", "Persistence forecast from live 24h history",
                    pts, local_tz=tz, raw=data)


def load_file(path: str) -> Forecast:
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "data" in data:          # raw GB API reply
        return Forecast("GB", "file", f"Saved GB API reply ({os.path.basename(path)})", _gb_points(data), raw=data)
    pts = [Point(parse_ts(r["start"]), float(r["gco2"])) for r in data]
    zone = "IN" if "india" in path.lower() else "GB"
    return Forecast(zone, "file", f"Saved forecast ({os.path.basename(path)})", pts,
                    local_tz=IST if zone == "IN" else UTC, raw=data)


def get_forecast(source: str, now: datetime) -> Forecast:
    """source: gb | india | em:ZONE | file:PATH"""
    if source == "gb":
        return fetch_gb(now)
    if source == "india":
        return india_model_forecast(now)
    if source.startswith("em:"):
        return fetch_em(source[3:], now)
    if source.startswith("file:"):
        return load_file(source[5:])
    raise ValueError(f"Unknown source '{source}'. Use gb, india, em:ZONE or file:PATH")

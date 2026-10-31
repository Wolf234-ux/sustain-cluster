"""
India grid carbon-intensity model (used when no live India API token is available).

Calibration, from published data:
  * Daily mean = 727 gCO2/kWh: CEA CO2 Baseline Database, weighted average
    grid emission factor for FY 2023-24 (0.727 tCO2/MWh).
  * Daily shape: fossil generation falls to ~125 GW at 1 pm and peaks at
    ~174 GW around 7 pm (Ember, H1 2026). Solar pushes fossil share down
    from late morning to late afternoon; the evening peak runs almost
    entirely on coal.

The hourly fossil-share curve below follows those anchor points. It is then
scaled so the 24-hour mean equals the CEA factor. This is a model of a
typical day, not a live measurement: weather, season and region move the
real curve. With an Electricity Maps token, CAS uses live data instead.
"""

from datetime import datetime, timedelta
from typing import List

from .models import IST, UTC, Forecast, Point

CEA_MEAN_GCO2 = 727.0  # FY 2023-24 weighted average, CEA

# Fossil share of generation by IST hour (0-23). Anchors: ~54% at 13:00, ~78% at 19:00-20:00.
FOSSIL_SHARE = [
    0.770, 0.770, 0.770, 0.770, 0.770, 0.775,   # 00-05 night, coal + some wind/hydro
    0.780, 0.765, 0.700, 0.640, 0.590, 0.560,   # 06-11 sun rises, solar ramps up
    0.545, 0.540, 0.550, 0.580, 0.630, 0.700,   # 12-17 solar peak then decline
    0.760, 0.785, 0.785, 0.780, 0.775, 0.770,   # 18-23 evening peak on coal
]


def hourly_profile() -> List[float]:
    """gCO2/kWh for each IST hour, scaled so the 24h mean equals the CEA factor."""
    mean_share = sum(FOSSIL_SHARE) / 24
    return [round(s / mean_share * CEA_MEAN_GCO2, 1) for s in FOSSIL_SHARE]


def india_model_forecast(now: datetime, hours: int = 48, step_min: int = 30) -> Forecast:
    profile = hourly_profile()
    start = now.astimezone(UTC).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % step_min)
    pts = []
    for i in range(int(hours * 60 / step_min)):
        t = start + timedelta(minutes=i * step_min)
        local = t.astimezone(IST)
        # linear interpolation between hourly values for a smooth 30-min curve
        h = local.hour + local.minute / 60
        h0, h1 = int(h) % 24, (int(h) + 1) % 24
        frac = h - int(h)
        g = profile[h0] * (1 - frac) + profile[h1] * frac
        pts.append(Point(t, round(g, 1)))
    return Forecast(
        zone="IN", source="india-model",
        method="India model: CEA FY24 mean 727 g/kWh, Ember 2026 hourly fossil shape",
        points=pts, local_tz=IST,
        raw={"profile_gco2_by_ist_hour": profile, "cea_mean": CEA_MEAN_GCO2},
    )

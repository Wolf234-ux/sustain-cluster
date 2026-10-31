"""
Electricity tariffs with time-of-day (ToD) pricing, so savings can be shown in rupees.

Gujarat presets come from the GERC tariff schedule for DGVCL / MGVCL / PGVCL /
UGVCL, effective 1 April 2026 (energy charges only; excludes electricity duty,
taxes, demand charges and FPPAS fuel surcharge):

  HTP-I  energy charge 400 / 420 / 430 paise per kWh (by billing-demand slab)
         ToU peak charge 07:00-11:00 and 18:00-22:00: +45 p (<=500 kVA), +85 p (>500 kVA)
         ToU discount 11:00-17:00: -60 p
  LTMD   energy charge 460 p; peak +45 p; discount 11:00-17:00 -60 p

The "india-tod" preset applies the national rule (Electricity (Rights of
Consumers) Amendment Rules, 2023): for C&I consumers of 10 kW and above, solar
hours are 10-20% cheaper and peak hours 10-20% dearer than the normal tariff.
Exact hours are set by each state commission; the preset uses illustrative
hours and should be adjusted.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Tuple

from .models import IST

Window = Tuple[int, int, float]  # (start_hour, end_hour, adjustment in Rs/kWh or fraction)


@dataclass
class Tariff:
    name: str
    base_rs: float                         # Rs per kWh
    tz: timezone = IST
    adders_rs: List[Window] = field(default_factory=list)    # absolute Rs/kWh changes
    multipliers: List[Window] = field(default_factory=list)  # fractional changes, e.g. -0.2
    note: str = ""

    def rate(self, t: datetime) -> float:
        h = t.astimezone(self.tz).hour
        r = self.base_rs
        for a, b, mult in self.multipliers:
            if a <= h < b:
                r *= 1 + mult
        for a, b, add in self.adders_rs:
            if a <= h < b:
                r += add
        return round(r, 4)


GJ_SRC = "GERC tariff schedule w.e.f. 1 Apr 2026 (energy charge only)"

PRESETS = {
    "gj-htp1-small": Tariff("Gujarat HTP-I, billing demand up to 500 kVA", 4.00,
                            adders_rs=[(7, 11, 0.45), (18, 22, 0.45), (11, 17, -0.60)], note=GJ_SRC),
    "gj-htp1": Tariff("Gujarat HTP-I, 500-2,500 kVA", 4.20,
                      adders_rs=[(7, 11, 0.85), (18, 22, 0.85), (11, 17, -0.60)], note=GJ_SRC),
    "gj-htp1-large": Tariff("Gujarat HTP-I, above 2,500 kVA", 4.30,
                            adders_rs=[(7, 11, 0.85), (18, 22, 0.85), (11, 17, -0.60)], note=GJ_SRC),
    "gj-ltmd": Tariff("Gujarat LTMD (load above 40 kW)", 4.60,
                      adders_rs=[(7, 11, 0.45), (18, 22, 0.45), (11, 17, -0.60)], note=GJ_SRC),
    "india-tod": Tariff("India national ToD rule (illustrative hours), base Rs 7/kWh", 7.00,
                        multipliers=[(9, 17, -0.20), (18, 23, 0.20)],
                        note="Electricity (Rights of Consumers) Amendment Rules 2023: solar hours "
                             "10-20% lower, peak 10-20% higher. Hours vary by state."),
}


def get_tariff(spec: str) -> Tariff:
    """'gj-htp1', 'india-tod', or 'flat:6.5' for a flat Rs/kWh rate. Empty -> None."""
    if not spec or spec == "none":
        return None
    if spec.startswith("flat:"):
        return Tariff(f"Flat Rs {float(spec[5:]):g}/kWh", float(spec[5:]))
    if spec not in PRESETS:
        raise ValueError(f"Unknown tariff '{spec}'. Options: {', '.join(PRESETS)} or flat:<rs>")
    return PRESETS[spec]

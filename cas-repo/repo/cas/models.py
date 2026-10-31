"""Core data types shared by every module."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has no daylight saving


def parse_ts(s: str) -> datetime:
    """Parse ISO timestamps such as 2026-10-31T05:00Z or 2026-10-31T05:00:00+00:00."""
    s = s.strip().replace("Z", "+00:00")
    if len(s) >= 16 and s[13] == ":" and (len(s) == 16 or s[16] in "+-"):
        s = s[:16] + ":00" + s[16:]  # add missing seconds
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Point:
    start: datetime   # UTC start of the slot
    gco2: float       # grams CO2 per kWh during the slot


@dataclass
class Forecast:
    zone: str                 # e.g. GB, IN, IN-WE
    source: str               # gb-api | electricitymaps | india-model | file
    method: str               # human description of how the numbers were produced
    points: List[Point]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    local_tz: timezone = UTC  # used for tariffs and display
    raw: Optional[object] = None  # untouched API payload, kept for the audit log

    @property
    def step_h(self) -> float:
        if len(self.points) < 2:
            return 1.0
        return (self.points[1].start - self.points[0].start).total_seconds() / 3600

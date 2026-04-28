from __future__ import annotations

from datetime import datetime
from datetime import timezone
from zoneinfo import ZoneInfo

ROME_TZ = ZoneInfo("Europe/Rome")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Naive datetimes are not allowed")
    return value.astimezone(timezone.utc)


def to_local(value: datetime, tz_name: str = "Europe/Rome") -> datetime:
    if value.tzinfo is None:
        raise ValueError("Naive datetimes are not allowed")
    return value.astimezone(ZoneInfo(tz_name))

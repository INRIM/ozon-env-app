"""Parser schedule calendar per il worker.

Mirror della grammatica in `app/scheduler/calendar_parser.py` (stesso subset V1),
piu la conversione verso trigger APScheduler. Tenuto qui per mantenere l'image
del worker autonoma. Ogni modifica alla grammatica va replicata in entrambi i
file e coperta da test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from datetime import time
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

DEFAULT_TIMEZONE = "Europe/Rome"
_CRON_FIELD_NAMES = ("minute", "hour", "day", "month", "day_of_week")
_CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}
_SYSTEMD_TIME_RE = re.compile(
    r"^(?P<hour>\*|\d{1,2}):(?P<minute>\d{1,2}):(?P<second>\d{1,2})$"
)


class CalendarParseError(ValueError):
    """Espressione calendar non supportata o invalida."""


@dataclass(frozen=True)
class ParsedSchedule:
    expression: str
    trigger_type: str
    timezone: str
    run_at: datetime | None = None
    cron_fields: dict[str, str] | None = None

    @property
    def is_one_shot(self) -> bool:
        return self.trigger_type == "date"

    def to_trigger(self):
        """Costruisce il trigger APScheduler corrispondente."""
        tz = ZoneInfo(self.timezone)
        if self.trigger_type == "date":
            return DateTrigger(run_date=self.run_at, timezone=tz)
        if self.trigger_type == "cron" and self.cron_fields:
            return CronTrigger(timezone=tz, **self.cron_fields)
        raise CalendarParseError(
            f"cannot build trigger for: {self.expression}"
        )


def _normalize_now(now: datetime | None, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _validate_cron_number(value: str, low: int, high: int) -> bool:
    if not value.isdigit():
        return False
    return low <= int(value) <= high


def _validate_cron_range(value: str, low: int, high: int) -> bool:
    if value == "*":
        return True
    if "-" not in value:
        return _validate_cron_number(value, low, high)
    start, end, *extra = value.split("-")
    if extra:
        return False
    if not (
        _validate_cron_number(start, low, high)
        and _validate_cron_number(end, low, high)
    ):
        return False
    return int(start) <= int(end)


def _validate_cron_field(value: str, low: int, high: int) -> bool:
    if not value:
        return False
    for token in value.split(","):
        if not token:
            return False
        base = token
        if "/" in token:
            base, step, *extra = token.split("/")
            if extra or not step.isdigit() or int(step) <= 0:
                return False
        if not _validate_cron_range(base, low, high):
            return False
    return True


def _parse_crontab_5(normalized: str, timezone: str) -> ParsedSchedule | None:
    parts = normalized.split()
    if len(parts) != 5:
        return None
    for value, (low, high) in zip(parts, _CRON_FIELD_RANGES, strict=True):
        if not _validate_cron_field(value, low, high):
            raise CalendarParseError(
                f"invalid crontab expression: {normalized}"
            )
    return ParsedSchedule(
        expression=normalized,
        trigger_type="cron",
        timezone=timezone,
        cron_fields=dict(zip(_CRON_FIELD_NAMES, parts, strict=True)),
    )


def _parse_systemd_daily_time(
    normalized: str, timezone: str
) -> ParsedSchedule | None:
    if normalized.startswith("*-*-* "):
        time_value = normalized.removeprefix("*-*-* ").strip()
    elif " " not in normalized and _SYSTEMD_TIME_RE.fullmatch(normalized):
        time_value = normalized
    else:
        return None

    match = _SYSTEMD_TIME_RE.fullmatch(time_value)
    if not match:
        raise CalendarParseError(
            f"invalid systemd daily expression: {normalized}"
        )

    hour = match.group("hour")
    minute = match.group("minute")
    second = match.group("second")
    if second != "00":
        raise CalendarParseError(
            "systemd daily expressions with non-zero seconds are not "
            "supported in V1"
        )
    if hour != "*" and not _validate_cron_number(hour, 0, 23):
        raise CalendarParseError(f"invalid systemd daily hour: {normalized}")
    if not _validate_cron_number(minute, 0, 59):
        raise CalendarParseError(f"invalid systemd daily minute: {normalized}")

    return ParsedSchedule(
        expression=normalized,
        trigger_type="cron",
        timezone=timezone,
        cron_fields={
            "minute": str(int(minute)),
            "hour": hour if hour == "*" else str(int(hour)),
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        },
    )


def parse_calendar_expression(
    expression: str,
    *,
    now: datetime | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> ParsedSchedule:
    normalized = str(expression or "").strip()
    if not normalized:
        raise CalendarParseError("calendar expression is empty")

    if normalized.lower() == "now":
        return ParsedSchedule(
            expression=normalized,
            trigger_type="date",
            timezone=timezone,
            run_at=_normalize_now(now, timezone),
        )

    iso_value = normalized
    if iso_value.endswith("Z"):
        iso_value = f"{iso_value[:-1]}+00:00"
    try:
        run_at = datetime.fromisoformat(iso_value)
    except ValueError:
        run_at = None
    if run_at is not None:
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=ZoneInfo(timezone))
        else:
            run_at = run_at.astimezone(ZoneInfo(timezone))
        return ParsedSchedule(
            expression=normalized,
            trigger_type="date",
            timezone=timezone,
            run_at=run_at,
        )

    try:
        date_value = datetime.fromisoformat(f"{normalized}T00:00:00")
    except ValueError:
        date_value = None
    if (
        date_value is not None
        and "T" not in normalized
        and " " not in normalized
    ):
        return ParsedSchedule(
            expression=normalized,
            trigger_type="date",
            timezone=timezone,
            run_at=datetime.combine(
                date_value.date(), time.min, tzinfo=ZoneInfo(timezone)
            ),
        )

    alias = _CRON_ALIASES.get(normalized.lower())
    if alias:
        parsed_alias = _parse_crontab_5(alias, timezone)
        if parsed_alias is None:
            raise CalendarParseError(
                f"invalid cron alias mapping: {normalized}"
            )
        return ParsedSchedule(
            expression=normalized,
            trigger_type=parsed_alias.trigger_type,
            timezone=timezone,
            cron_fields=parsed_alias.cron_fields,
        )

    systemd_daily = _parse_systemd_daily_time(normalized, timezone)
    if systemd_daily is not None:
        return systemd_daily

    cron_schedule = _parse_crontab_5(normalized, timezone)
    if cron_schedule is not None:
        return cron_schedule

    raise CalendarParseError(f"unsupported calendar expression: {normalized}")

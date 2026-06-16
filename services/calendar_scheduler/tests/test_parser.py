from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from calendar_scheduler.parser import CalendarParseError
from calendar_scheduler.parser import parse_calendar_expression


def test_now_is_one_shot_date_trigger():
    parsed = parse_calendar_expression("now")
    assert parsed.is_one_shot is True
    assert isinstance(parsed.to_trigger(), DateTrigger)


def test_iso_datetime_converts_to_app_timezone():
    parsed = parse_calendar_expression("2026-06-15T08:30:00Z")
    assert parsed.run_at == datetime(
        2026, 6, 15, 10, 30, 0, tzinfo=ZoneInfo("Europe/Rome")
    )


def test_crontab_5_fields_builds_cron_trigger():
    parsed = parse_calendar_expression("10 3 * * *")
    assert parsed.cron_fields["minute"] == "10"
    assert parsed.cron_fields["hour"] == "3"
    assert isinstance(parsed.to_trigger(), CronTrigger)


def test_alias_daily():
    parsed = parse_calendar_expression("@daily")
    assert parsed.cron_fields == {
        "minute": "0",
        "hour": "0",
        "day": "*",
        "month": "*",
        "day_of_week": "*",
    }


def test_systemd_daily_time():
    parsed = parse_calendar_expression("*-*-* 03:10:00")
    assert parsed.cron_fields["hour"] == "3"
    assert parsed.cron_fields["minute"] == "10"


def test_systemd_compact_daily_time():
    parsed = parse_calendar_expression("03:10:00")
    assert parsed.cron_fields["hour"] == "3"


def test_systemd_non_zero_seconds_rejected():
    with pytest.raises(CalendarParseError, match="seconds"):
        parse_calendar_expression("*-*-* 03:10:05")


def test_unsupported_expression_rejected():
    with pytest.raises(CalendarParseError, match="unsupported"):
        parse_calendar_expression("Mon..Fri 03:10:00")


def test_empty_expression_rejected():
    with pytest.raises(CalendarParseError, match="empty"):
        parse_calendar_expression("")

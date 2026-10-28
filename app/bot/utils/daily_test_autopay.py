from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DAILY_TEST_AUTOPAY_AMOUNT = 10
DAILY_TEST_AUTOPAY_WINDOW_START_HOUR = 6
DAILY_TEST_AUTOPAY_WINDOW_END_HOUR = 22
DAILY_TEST_AUTOPAY_MAX_FAILURES = 3
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def moscow_date(now_utc: datetime) -> date:
    aware_utc = now_utc.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(MOSCOW_TIMEZONE).date()


def is_moscow_charge_window(now_utc: datetime) -> bool:
    aware_utc = now_utc.replace(tzinfo=timezone.utc)
    local_hour = aware_utc.astimezone(MOSCOW_TIMEZONE).hour
    return (
        DAILY_TEST_AUTOPAY_WINDOW_START_HOUR
        <= local_hour
        < DAILY_TEST_AUTOPAY_WINDOW_END_HOUR
    )


def random_charge_at_for_moscow_date(
    target_date: date,
    *,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> datetime:
    window_minutes = (
        DAILY_TEST_AUTOPAY_WINDOW_END_HOUR
        - DAILY_TEST_AUTOPAY_WINDOW_START_HOUR
    ) * 60
    offset_minutes = randbelow(window_minutes)
    local_datetime = datetime.combine(
        target_date,
        time(hour=DAILY_TEST_AUTOPAY_WINDOW_START_HOUR),
        tzinfo=MOSCOW_TIMEZONE,
    ) + timedelta(minutes=offset_minutes)
    return local_datetime.astimezone(timezone.utc).replace(tzinfo=None)


def next_random_charge_at(
    after_utc: datetime,
    *,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> datetime:
    target_date = moscow_date(after_utc) + timedelta(days=1)
    return random_charge_at_for_moscow_date(target_date, randbelow=randbelow)


def format_moscow_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    aware_utc = value.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(MOSCOW_TIMEZONE).strftime("%d.%m.%Y в %H:%M МСК")

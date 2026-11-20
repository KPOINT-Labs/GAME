import datetime
from zoneinfo import ZoneInfo

import pytest

from app.util.timezones import (
    DEFAULT_GAME_TIMEZONE,
    local_datetime,
    validate_timezone_name,
    zone_of,
)


@pytest.mark.parametrize("name", ["UTC", "Asia/Kolkata", "America/Los_Angeles"])
def test_validate_accepts_iana_names(name):
    assert validate_timezone_name(name) == name


def test_validate_passes_none_through():
    assert validate_timezone_name(None) is None


@pytest.mark.parametrize("name", ["", "  ", "Mars/Olympus", "+05:30", "IST", 5])
def test_validate_rejects_unknown_names(name):
    with pytest.raises(ValueError):
        validate_timezone_name(name)


def test_zone_of_defaults_and_falls_back_to_utc():
    assert zone_of(None) == ZoneInfo(DEFAULT_GAME_TIMEZONE)
    assert zone_of("Asia/Kolkata") == ZoneInfo("Asia/Kolkata")
    # A stored name that no longer resolves keeps scoring in UTC.
    assert zone_of("Mars/Olympus") == ZoneInfo("UTC")


def test_local_datetime_treats_naive_values_as_utc():
    naive = datetime.datetime(2026, 11, 17, 20, 0)
    local = local_datetime(naive, ZoneInfo("Asia/Kolkata"))

    assert (local.date(), local.hour, local.minute) == (
        datetime.date(2026, 11, 18),
        1,
        30,
    )

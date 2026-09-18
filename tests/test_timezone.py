from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from revshare.notifications import reporting_period
from revshare.timezone import format_local_datetime, local_today


def test_local_today_uses_yerevan_calendar():
    yerevan = ZoneInfo("Asia/Yerevan")
    expected = datetime.now(yerevan).date()
    assert local_today() == expected


def test_reporting_period_defaults_to_yerevan_today():
    yerevan = ZoneInfo("Asia/Yerevan")
    today = datetime.now(yerevan).date()
    assert reporting_period() == reporting_period(today)


def test_format_local_datetime_shifts_from_utc():
    assert format_local_datetime("2026-09-01T20:30:00+00:00") == "2026-09-02 00:30"

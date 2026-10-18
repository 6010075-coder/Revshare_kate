from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from revshare.notifications import reporting_period
from revshare.timezone import (
    format_local_datetime,
    local_today,
    next_notice_send_at,
    seconds_until_notice_send,
)


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


def test_next_notice_send_is_today_before_1115():
    yerevan = ZoneInfo("Asia/Yerevan")
    now = datetime(2026, 10, 18, 10, 0, tzinfo=yerevan)
    assert next_notice_send_at(now) == datetime(2026, 10, 18, 11, 15, tzinfo=yerevan)
    assert seconds_until_notice_send(now) == 75 * 60


def test_next_notice_send_is_tomorrow_after_1115():
    yerevan = ZoneInfo("Asia/Yerevan")
    now = datetime(2026, 10, 18, 11, 16, tzinfo=yerevan)
    assert next_notice_send_at(now) == datetime(2026, 10, 19, 11, 15, tzinfo=yerevan)


def test_next_notice_send_at_exactly_1115_waits_until_tomorrow():
    yerevan = ZoneInfo("Asia/Yerevan")
    now = datetime(2026, 10, 18, 11, 15, tzinfo=yerevan)
    assert next_notice_send_at(now) == datetime(2026, 10, 19, 11, 15, tzinfo=yerevan)

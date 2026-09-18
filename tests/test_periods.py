from datetime import date

from revshare.notifications import previous_period, recent_periods, reporting_period


def test_reporting_period_is_the_month_that_just_closed():
    assert reporting_period(date(2026, 9, 15)) == "2026-08"
    assert reporting_period(date(2026, 1, 3)) == "2025-12"
    assert previous_period("2026-01") == "2025-12"


def test_recent_periods_start_with_the_closed_month():
    periods = recent_periods(count=4, today=date(2026, 9, 15))
    assert periods == ["2026-08", "2026-07", "2026-06", "2026-05"]


def test_recent_periods_keep_an_older_selection():
    periods = recent_periods("2024-02", count=3, today=date(2026, 9, 15))
    assert periods[0] == "2026-08"
    assert periods[-1] == "2024-02"
    assert periods == sorted(set(periods), reverse=True)

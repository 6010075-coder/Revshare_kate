from datetime import date

from revshare.notifications import (
    deadline_date,
    default_notice_settings,
    normalize_notice_settings,
    schedule_for_period,
    weekday_on_or_after,
)


def test_weekend_dates_move_to_monday():
    assert weekday_on_or_after(date(2026, 9, 5)) == date(2026, 9, 7)
    assert weekday_on_or_after(date(2026, 9, 6)) == date(2026, 9, 7)
    assert weekday_on_or_after(date(2026, 9, 4)) == date(2026, 9, 4)


def test_default_august_collection_in_september_2026():
    events = {item["key"]: item["send_on"] for item in schedule_for_period({}, "2026-08")}
    assert events["invite"] == date(2026, 9, 1)
    assert events["reminder"] == date(2026, 9, 4)
    assert events["last"] == date(2026, 9, 7)
    assert events["digest"] == date(2026, 9, 8)
    assert events["overdue"] == date(2026, 9, 9)
    assert deadline_date({}, "2026-08") == date(2026, 9, 7)


def test_default_texts_follow_the_partner_language():
    russian = default_notice_settings("ru")
    invite = next(item for item in russian["notices"] if item["key"] == "invite")
    assert invite["message"].startswith("Здравствуйте")

    switched = normalize_notice_settings({**russian, "language": "uk"})
    invite = next(item for item in switched["notices"] if item["key"] == "invite")
    assert invite["message"].startswith("Вітаємо")

    custom = normalize_notice_settings(
        {"language": "ru", "notices": [{"key": "invite", "message": "Свой текст {link}"}]}
    )
    invite = next(item for item in custom["notices"] if item["key"] == "invite")
    assert invite["message"] == "Свой текст {link}"


def test_partner_can_disable_a_notice_and_change_the_day():
    settings = default_notice_settings()
    for notice in settings["notices"]:
        if notice["key"] == "reminder":
            notice["enabled"] = False
        if notice["key"] == "last":
            notice["day"] = 10
    events = {item["key"]: item["send_on"] for item in schedule_for_period(settings, "2026-08")}
    assert "reminder" not in events
    assert events["last"] == date(2026, 9, 10)


def test_legacy_custom_days_become_a_personal_schedule():
    stored = default_notice_settings()
    for notice in stored["notices"]:
        if notice["key"] == "invite":
            notice["day"] = 3
    shared = {
        "notices": [
            {"key": "invite", "day": 8, "enabled": True},
            {"key": "last", "day": 11, "enabled": True},
        ]
    }
    settings = normalize_notice_settings(stored, global_schedule=shared)
    assert settings["schedule_override"] is True
    invite = next(item for item in settings["notices"] if item["key"] == "invite")
    assert invite["day"] == 3

    matching = normalize_notice_settings(
        default_notice_settings(), global_schedule=shared
    )
    assert matching["schedule_override"] is False
    invite = next(item for item in matching["notices"] if item["key"] == "invite")
    assert invite["day"] == 8


def test_shared_schedule_is_used_until_a_partner_overrides_it():
    shared = {
        "notices": [
            {"key": "invite", "day": 8, "enabled": True},
            {"key": "reminder", "day": 9, "enabled": True},
            {"key": "last", "day": 11, "enabled": True},
            {"key": "digest", "day": 12, "enabled": True},
            {"key": "overdue", "day": 13, "enabled": True},
        ]
    }
    inherited = normalize_notice_settings(
        {"language": "en", "schedule_override": False},
        global_schedule=shared,
    )
    invite = next(item for item in inherited["notices"] if item["key"] == "invite")
    assert invite["day"] == 8

    personal = normalize_notice_settings(
        {
            "language": "en",
            "schedule_override": True,
            "notices": [{"key": "invite", "day": 2, "enabled": True}],
        },
        global_schedule=shared,
    )
    invite = next(item for item in personal["notices"] if item["key"] == "invite")
    assert invite["day"] == 2

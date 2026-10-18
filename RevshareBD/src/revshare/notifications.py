from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from revshare.i18n import SUPPORTED_LANGUAGES
from revshare.timezone import local_today

NOTICE_KEYS = ("invite", "reminder", "last", "digest", "overdue")
PARTNER_NOTICE_KEYS = ("invite", "reminder", "last", "overdue")


def reporting_period(today: date | None = None) -> str:
    today = today or local_today()
    year = today.year if today.month > 1 else today.year - 1
    month = today.month - 1 if today.month > 1 else 12
    return f"{year:04d}-{month:02d}"


def previous_period(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def recent_periods(
    selected: str = "", count: int = 24, today: date | None = None
) -> list[str]:
    """Reporting months, newest first, always including the selected one."""
    period = reporting_period(today)
    periods = []
    for _ in range(max(1, count)):
        periods.append(period)
        period = previous_period(period)
    if selected and selected not in periods:
        periods.append(selected)
    return sorted(set(periods), reverse=True)

DEFAULT_DAYS = {
    "invite": 1,
    "reminder": 4,
    "last": 5,
    "digest": 6,
    "overdue": 7,
}

NOTICE_LANGUAGES = SUPPORTED_LANGUAGES
DEFAULT_NOTICE_LANGUAGE = "en"

DEFAULT_MESSAGES = {
    "en": {
        "invite": (
            "Hello! Please upload the {name} report for {period} by {deadline} inclusive.\n"
            "{link}"
        ),
        "reminder": (
            "Reminder: the {name} report for {period} has not been received yet. "
            "Please upload it by {deadline} inclusive.\n"
            "{link}"
        ),
        "last": (
            "Today is the last day to submit the {name} report for {period}. "
            "Please upload it by the end of the day.\n"
            "{link}"
        ),
        "digest": "",
        "overdue": (
            "The deadline for the {name} report for {period} was {deadline}. "
            "Please upload the report as soon as possible.\n"
            "{link}"
        ),
    },
    "ru": {
        "invite": (
            "Здравствуйте! Пожалуйста, загрузите отчёт по приложению {name} "
            "за {period} до {deadline} включительно.\n"
            "{link}"
        ),
        "reminder": (
            "Напоминаем, что отчёт по приложению {name} за {period} ещё не получен. "
            "Пожалуйста, загрузите его до {deadline} включительно.\n"
            "{link}"
        ),
        "last": (
            "Сегодня последний день подачи отчёта по приложению {name} за {period}. "
            "Пожалуйста, загрузите его до конца дня.\n"
            "{link}"
        ),
        "digest": "",
        "overdue": (
            "Срок подачи отчёта по приложению {name} за {period} истёк {deadline}. "
            "Пожалуйста, загрузите отчёт как можно скорее.\n"
            "{link}"
        ),
    },
    "uk": {
        "invite": (
            "Вітаємо! Будь ласка, завантажте звіт щодо застосунку {name} "
            "за {period} до {deadline} включно.\n"
            "{link}"
        ),
        "reminder": (
            "Нагадуємо, що звіт щодо застосунку {name} за {period} ще не отримано. "
            "Будь ласка, завантажте його до {deadline} включно.\n"
            "{link}"
        ),
        "last": (
            "Сьогодні останній день подання звіту щодо застосунку {name} за {period}. "
            "Будь ласка, завантажте його до кінця дня.\n"
            "{link}"
        ),
        "digest": "",
        "overdue": (
            "Термін подання звіту щодо застосунку {name} за {period} завершився {deadline}. "
            "Будь ласка, завантажте звіт якнайшвидше.\n"
            "{link}"
        ),
    },
    "pl": {
        "invite": (
            "Dzień dobry! Proszę wgrać raport {name} za {period} do {deadline} włącznie.\n"
            "{link}"
        ),
        "reminder": (
            "Przypominamy, że raport {name} za {period} jeszcze nie wpłynął. "
            "Proszę wgrać go do {deadline} włącznie.\n"
            "{link}"
        ),
        "last": (
            "Dziś mija termin złożenia raportu {name} za {period}. "
            "Proszę wgrać go do końca dnia.\n"
            "{link}"
        ),
        "digest": "",
        "overdue": (
            "Termin złożenia raportu {name} za {period} minął {deadline}. "
            "Proszę wgrać raport jak najszybciej.\n"
            "{link}"
        ),
    },
    "hu": {
        "invite": (
            "Üdvözöljük! Kérjük, töltse fel a(z) {name} {period} jelentését {deadline}-ig bezárólag.\n"
            "{link}"
        ),
        "reminder": (
            "Emlékeztető: a(z) {name} {period} jelentése még nem érkezett meg. "
            "Kérjük, töltse fel {deadline}-ig bezárólag.\n"
            "{link}"
        ),
        "last": (
            "Ma van a határidő a(z) {name} {period} jelentésének benyújtására. "
            "Kérjük, töltse fel a nap végéig.\n"
            "{link}"
        ),
        "digest": "",
        "overdue": (
            "A(z) {name} {period} jelentésének határideje {deadline} volt. "
            "Kérjük, töltse fel a jelentést mielőbb.\n"
            "{link}"
        ),
    },
    "pt": {
        "invite": (
            "Olá! Por favor, carregue o relatório de {name} referente a {period} até {deadline}, inclusive.\n"
            "{link}"
        ),
        "reminder": (
            "Lembrete: o relatório de {name} referente a {period} ainda não chegou. "
            "Por favor, carregue-o até {deadline}, inclusive.\n"
            "{link}"
        ),
        "last": (
            "Hoje é o último dia para enviar o relatório de {name} referente a {period}. "
            "Por favor, carregue-o até ao fim do dia.\n"
            "{link}"
        ),
        "digest": "",
        "overdue": (
            "O prazo do relatório de {name} referente a {period} terminou em {deadline}. "
            "Por favor, carregue o relatório o mais depressa possível.\n"
            "{link}"
        ),
    },
}

ALL_DEFAULT_MESSAGES = {
    message.strip()
    for language in DEFAULT_MESSAGES.values()
    for message in language.values()
    if message.strip()
}


def notice_language(value: str | None) -> str:
    return value if value in NOTICE_LANGUAGES else DEFAULT_NOTICE_LANGUAGE


def default_message(key: str, language: str) -> str:
    return DEFAULT_MESSAGES[notice_language(language)][key]


def default_schedule() -> dict[str, Any]:
    return {
        "weekend_shift": True,
        "notices": [
            {
                "key": key,
                "enabled": True,
                "day": DEFAULT_DAYS[key],
                "audience": "internal" if key == "digest" else "partner",
            }
            for key in NOTICE_KEYS
        ],
    }


def schedule_signature(raw: dict[str, Any] | None) -> tuple[tuple[str, bool, int], ...]:
    return tuple(
        (item["key"], item["enabled"], item["day"])
        for item in normalize_schedule(raw)["notices"]
    )


def normalize_schedule(raw: dict[str, Any] | None) -> dict[str, Any]:
    incoming = raw or {}
    defaults = default_schedule()
    by_key = {
        item["key"]: item
        for item in incoming.get("notices", [])
        if isinstance(item, dict) and item.get("key") in NOTICE_KEYS
    }
    notices = []
    for default in defaults["notices"]:
        item = {**default, **by_key.get(default["key"], {})}
        try:
            day = int(item.get("day") or default["day"])
        except (TypeError, ValueError):
            day = default["day"]
        item["day"] = min(28, max(1, day))
        item["enabled"] = bool(item.get("enabled", True))
        item["audience"] = default["audience"]
        notices.append(
            {
                "key": item["key"],
                "enabled": item["enabled"],
                "day": item["day"],
                "audience": item["audience"],
            }
        )
    return {"weekend_shift": True, "notices": notices}


def default_notice_settings(language: str = DEFAULT_NOTICE_LANGUAGE) -> dict[str, Any]:
    language = notice_language(language)
    return {
        "language": language,
        "weekend_shift": True,
        "notices": [
            {
                "key": key,
                "enabled": True,
                "day": DEFAULT_DAYS[key],
                "audience": "internal" if key == "digest" else "partner",
                "message": default_message(key, language),
            }
            for key in NOTICE_KEYS
        ],
    }


def normalize_notice_settings(
    raw: dict[str, Any] | None,
    global_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    incoming = raw or {}
    language = notice_language(incoming.get("language"))
    defaults = default_notice_settings(language)
    shared = normalize_schedule(global_schedule)
    complete = {
        item.get("key")
        for item in incoming.get("notices", [])
        if isinstance(item, dict)
    } >= set(NOTICE_KEYS)
    # A resolved snapshot (send/preview) already has every notice. Use those days.
    if global_schedule is None and complete:
        shared = normalize_schedule(incoming)
        override = True
        stored_override = (
            bool(incoming.get("schedule_override"))
            if "schedule_override" in incoming
            else True
        )
    elif "schedule_override" in incoming:
        override = bool(incoming.get("schedule_override"))
        stored_override = override
    elif complete:
        override = schedule_signature(incoming) != schedule_signature(None)
        stored_override = override
    else:
        override = False
        stored_override = False
    by_key = {
        item["key"]: item
        for item in incoming.get("notices", [])
        if isinstance(item, dict) and item.get("key") in NOTICE_KEYS
    }
    notices = []
    for default in defaults["notices"]:
        shared_item = next(
            item for item in shared["notices"] if item["key"] == default["key"]
        )
        item = {**default, **shared_item}
        stored = by_key.get(default["key"], {})
        if override:
            item = {**item, **stored}
        elif stored.get("message"):
            item["message"] = stored["message"]
        try:
            day = int(item.get("day") or shared_item["day"])
        except (TypeError, ValueError):
            day = shared_item["day"]
        item["day"] = min(28, max(1, day))
        item["enabled"] = bool(item.get("enabled", shared_item["enabled"]))
        item["audience"] = default["audience"]
        message = str(item.get("message") or "").strip()
        if not message or message in ALL_DEFAULT_MESSAGES:
            message = default["message"]
        item["message"] = message
        notices.append(item)
    return {
        "language": language,
        "weekend_shift": True,
        "schedule_override": stored_override,
        "notices": notices,
    }


def weekday_on_or_after(value: date) -> date:
    if value.weekday() == 5:
        return value + timedelta(days=2)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def collection_month(period: str) -> tuple[int, int]:
    year, month = (int(part) for part in period.split("-"))
    if month == 12:
        return year + 1, 1
    return year, month + 1


def date_in_month(year: int, month: int, day: int) -> date:
    last_day = monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def planned_date(year: int, month: int, day: int, weekend_shift: bool) -> date:
    value = date_in_month(year, month, day)
    return weekday_on_or_after(value) if weekend_shift else value


def deadline_date(settings: dict[str, Any], period: str) -> date:
    settings = normalize_notice_settings(settings)
    year, month = collection_month(period)
    last = next(item for item in settings["notices"] if item["key"] == "last")
    return planned_date(year, month, last["day"], settings["weekend_shift"])


def collapse_same_day(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    partner_rank = {"invite": 1, "reminder": 2, "last": 3, "overdue": 4}
    kept: list[dict[str, Any]] = []
    used_days: set[date] = set()
    for event in sorted(
        events,
        key=lambda item: (item["send_on"], -partner_rank.get(item["key"], 0)),
    ):
        if event["audience"] != "partner":
            kept.append(event)
            continue
        if event["send_on"] in used_days:
            continue
        used_days.add(event["send_on"])
        kept.append(event)
    return sorted(kept, key=lambda item: (item["send_on"], item["key"]))


def schedule_for_period(
    settings: dict[str, Any],
    period: str,
    *,
    name: str = "{name}",
    link: str = "{link}",
) -> list[dict[str, Any]]:
    settings = normalize_notice_settings(settings)
    year, month = collection_month(period)
    deadline = deadline_date(settings, period)
    events = []
    for notice in settings["notices"]:
        if not notice["enabled"]:
            continue
        send_on = planned_date(
            year, month, notice["day"], settings["weekend_shift"]
        )
        events.append(
            {
                "key": notice["key"],
                "audience": notice["audience"],
                "send_on": send_on,
                "message": format_notice(
                    notice["message"],
                    name=name,
                    period=period,
                    deadline=deadline.isoformat(),
                    link=link,
                ),
            }
        )
    partner_events = [item for item in events if item["audience"] == "partner"]
    internal_events = [item for item in events if item["audience"] != "partner"]
    digest = next((item for item in internal_events if item["key"] == "digest"), None)
    if digest and digest["send_on"] <= deadline:
        digest["send_on"] = weekday_on_or_after(deadline + timedelta(days=1))
    overdue = next((item for item in partner_events if item["key"] == "overdue"), None)
    digest_day = digest["send_on"] if digest else deadline
    if overdue and overdue["send_on"] <= digest_day:
        overdue["send_on"] = weekday_on_or_after(digest_day + timedelta(days=1))
    partner_events = [
        item
        for item in collapse_same_day(partner_events)
        if not (item["key"] == "reminder" and item["send_on"] >= deadline)
    ]
    return sorted(partner_events + internal_events, key=lambda item: item["send_on"])


def format_notice(template: str, **values: str) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
        text = text.replace("{{" + key + "}}", value)
    return text


def settings_from_form(
    weekend_shift: bool,
    enabled_keys: list[str],
    days: dict[str, str],
    messages: dict[str, str],
    language: str = DEFAULT_NOTICE_LANGUAGE,
    schedule_override: bool = False,
    global_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    language = notice_language(language)
    enabled = set(enabled_keys)
    defaults = default_notice_settings(language)["notices"]
    notices = []
    for key in NOTICE_KEYS:
        default = next(item for item in defaults if item["key"] == key)
        raw_day = days.get(key, str(default["day"]))
        try:
            day = int(raw_day)
        except ValueError:
            day = default["day"]
        notices.append(
            {
                "key": key,
                "enabled": key in enabled,
                "day": min(28, max(1, day)),
                "audience": default["audience"],
                "message": messages.get(key, default["message"]),
            }
        )
    return normalize_notice_settings(
        {
            "language": language,
            "weekend_shift": weekend_shift,
            "schedule_override": schedule_override,
            "notices": notices,
        },
        global_schedule=global_schedule,
    )

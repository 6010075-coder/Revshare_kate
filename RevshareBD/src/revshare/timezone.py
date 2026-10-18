from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

YEREVAN_TZ = ZoneInfo("Asia/Yerevan")
NOTICE_SEND_HOUR = 11
NOTICE_SEND_MINUTE = 15


def local_today() -> date:
    """Calendar day in Yerevan, used for reporting period and scheduled notices."""
    return datetime.now(YEREVAN_TZ).date()


def _as_local(now: datetime | None = None) -> datetime:
    instant = now or datetime.now(YEREVAN_TZ)
    if instant.tzinfo is None:
        return instant.replace(tzinfo=YEREVAN_TZ)
    return instant.astimezone(YEREVAN_TZ)


def next_notice_send_at(now: datetime | None = None) -> datetime:
    """Next 11:15 in GMT+4. If that time has already started today, return tomorrow."""
    instant = _as_local(now)
    target = instant.replace(
        hour=NOTICE_SEND_HOUR, minute=NOTICE_SEND_MINUTE, second=0, microsecond=0
    )
    if instant >= target:
        target = target + timedelta(days=1)
    return target


def seconds_until_notice_send(now: datetime | None = None) -> float:
    instant = _as_local(now)
    return max(0.0, (next_notice_send_at(instant) - instant).total_seconds())


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def format_local_datetime(iso_timestamp: str | None) -> str:
    if not iso_timestamp:
        return "—"
    try:
        normalized = iso_timestamp.replace("Z", "+00:00")
        instant = datetime.fromisoformat(normalized)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        return instant.astimezone(YEREVAN_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_timestamp[:16].replace("T", " ")

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

YEREVAN_TZ = ZoneInfo("Asia/Yerevan")


def local_today() -> date:
    """Calendar day in Yerevan, used for reporting period and scheduled notices."""
    return datetime.now(YEREVAN_TZ).date()


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

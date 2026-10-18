from __future__ import annotations

import json
import secrets
import sqlite3
from urllib.parse import urlparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from revshare.countries import canonical_app_category
from revshare.extract import BaseRow
from revshare.fx import DEFAULT_USD_RATES, SUPPORTED_CURRENCIES, to_usd, valid_period
from revshare.timezone import local_today
from revshare.notifications import (
    NOTICE_KEYS,
    reporting_period,
    previous_period,
    PARTNER_NOTICE_KEYS,
    default_message,
    normalize_notice_settings,
    normalize_schedule,
    notice_language,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "revshare.db"


@dataclass
class Review:
    level: str
    score: int
    reasons: list[dict[str, object]]
    connected: int
    active: int
    reported: int
    missing_active: list[str]
    unknown: list[str]


CLAIM_REASON_KEYS = {
    "no_payment",
    "free_trial",
    "pay_next_month",
    "disconnected_before_month",
    "connected_after_month",
    "file_error",
    "other",
}

NOTICE_CLAIM_TIMEOUT = timedelta(minutes=15)


def discrepancy_items(
    reported_ids: list[str],
    connected_ids: set[str],
    active_ids: set[str],
) -> list[dict[str, object]]:
    reported = set(reported_ids)
    items: list[dict[str, object]] = []
    for location_id in sorted(active_ids - reported):
        items.append(
            {
                "location_id": location_id,
                "kind": "active_not_reported",
                "required": True,
            }
        )
    for location_id in sorted((connected_ids - active_ids) - reported):
        items.append(
            {
                "location_id": location_id,
                "kind": "connected_not_active",
                "required": False,
            }
        )
    for location_id in sorted(reported - connected_ids) if connected_ids else []:
        items.append(
            {
                "location_id": location_id,
                "kind": "reported_not_connected",
                "required": False,
            }
        )
    return items


def verify_claim(
    kind: str,
    reason: str,
    is_connected: bool,
    is_active: bool,
    had_payment: bool,
) -> tuple[str, str]:
    if had_payment and reason in {"no_payment", "free_trial", "pay_next_month"}:
        return "contradicted", "payment_found"
    if reason == "disconnected_before_month" and (is_connected or is_active):
        return "contradicted", "usage_after_claimed_disconnect"
    if reason == "connected_after_month" and is_active:
        return "contradicted", "activity_in_report_month"
    if reason == "file_error":
        return "contradicted", "file_still_has_difference"
    if reason == "unexplained":
        if had_payment:
            return "review", "payment_found_unexplained"
        return "review", "awaiting_partner_reason"
    if reason == "pay_next_month":
        return "follow_up", "check_next_report"
    if reason in {"no_payment", "free_trial"} and is_active:
        return "review", "activity_without_payment"
    if kind == "reported_not_connected":
        return "review", "reported_connection_not_found"
    return "review", "no_independent_proof"


def normalize_ids(raw: str) -> list[str]:
    values = raw.replace(",", "\n").replace(";", "\n").splitlines()
    return [value.strip() for value in values if value.strip()]


def normalize_location_detail(raw: dict[str, object] | None) -> dict[str, object]:
    if not raw:
        return {
            "location_name": "",
            "connected_at": "",
            "last_api_at": "",
            "has_api_amounts": 0,
        }
    has_amounts = raw.get("has_api_amounts", raw.get("api_amounts"))
    if isinstance(has_amounts, str):
        has_amounts = has_amounts.strip().lower() in {"1", "true", "yes", "y"}
    elif has_amounts is None:
        has_amounts = 0
    else:
        has_amounts = int(bool(has_amounts))
    return {
        "location_name": str(
            raw.get("name") or raw.get("location_name") or ""
        ).strip(),
        "connected_at": str(
            raw.get("connected_at") or raw.get("connected") or ""
        ).strip(),
        "last_api_at": str(
            raw.get("last_api_at")
            or raw.get("last_api_activity")
            or raw.get("last_activity_at")
            or ""
        ).strip(),
        "has_api_amounts": has_amounts,
    }


def as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_application_links(
    urls: str | list[str] | None,
    comments: str | list[str] | None = None,
    categories: str | list[str] | None = None,
) -> list[dict[str, str]]:
    url_values = as_list(urls)
    comment_values = as_list(comments)
    category_values = as_list(categories)
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_url in enumerate(url_values):
        url = raw_url.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid application URL: {url}")
        if url in seen:
            continue
        seen.add(url)
        comment = ""
        if index < len(comment_values):
            comment = comment_values[index].strip()
        category = (
            canonical_app_category(category_values[index])
            if index < len(category_values)
            else ""
        )
        links.append({"url": url, "comment": comment, "category": category})
    return links


def _flatten_line_items(
    application_breakdown: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    """Return the flat list of per-row line items across all groups.

    The parser stores line items nested inside each application_breakdown
    group (``group["line_items"]``); for SQL access we also persist them
    at the top level of ``report_versions.line_items`` so reviewers can
    see individual customer details without parsing the breakdown JSON.
    """
    if not application_breakdown:
        return []
    flat: list[dict[str, object]] = []
    for group in application_breakdown:
        if not isinstance(group, dict):
            continue
        items = group.get("line_items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                flat.append(item)
    return flat


def assess(
    reported_ids: list[str],
    connected_ids: set[str],
    active_ids: set[str],
    *,
    turnover: float | None = None,
    reported_share: float | None = None,
    contract_rate: float | None = None,
    previous_turnover: float | None = None,
    previous_client_count: int | None = None,
) -> Review:
    unique_reported = set(reported_ids)
    duplicates = len(reported_ids) - len(unique_reported)
    missing_active = sorted(active_ids - unique_reported)
    unknown = sorted(unique_reported - connected_ids) if connected_ids else []
    score = 0
    reasons: list[dict[str, object]] = []

    if not reported_ids:
        score += 50
        reasons.append({"key": "reason_no_registry"})
    if duplicates:
        score += min(20, duplicates * 5)
        reasons.append({"key": "reason_duplicates", "count": duplicates})
    if missing_active:
        ratio = len(missing_active) / max(1, len(active_ids))
        score += 45 if ratio > 0.25 else 25 if ratio > 0.1 else 10
        reasons.append({"key": "reason_missing_active", "count": len(missing_active)})
    if unknown:
        score += min(25, 5 + len(unknown) * 2)
        reasons.append({"key": "reason_unknown", "count": len(unknown)})
    if connected_ids and len(unique_reported) > len(connected_ids) * 1.15:
        score += 15
        reasons.append({"key": "reason_too_many"})
    if not connected_ids and not active_ids:
        score += 15
        reasons.append({"key": "reason_no_snapshot"})
    if turnover is not None:
        if contract_rate is None:
            score += 15
            reasons.append({"key": "reason_rate_missing"})
        elif reported_share is not None:
            expected_share = round(turnover * contract_rate, 2)
            tolerance = max(0.01, abs(expected_share) * 0.01)
            if abs(reported_share - expected_share) > tolerance:
                score += 30
                reasons.append(
                    {
                        "key": "reason_share_mismatch",
                        "expected": expected_share,
                        "reported": reported_share,
                    }
                )
    if (
        previous_turnover is not None
        and previous_turnover > 0
        and turnover is not None
        and previous_client_count
        and unique_reported
    ):
        client_change = abs(len(unique_reported) - previous_client_count) / max(
            1, previous_client_count
        )
        turnover_change = (turnover - previous_turnover) / previous_turnover
        if client_change <= 0.2 and turnover_change <= -0.5:
            score += 25
            reasons.append({"key": "reason_turnover_drop"})
        elif client_change <= 0.2 and turnover_change >= 1:
            score += 15
            reasons.append({"key": "reason_turnover_spike"})

    score = min(score, 100)
    level = "red" if score >= 50 else "yellow" if score >= 15 else "green"
    if not reasons:
        reasons.append({"key": "reason_clear"})
    return Review(
        level=level,
        score=score,
        reasons=reasons,
        connected=len(connected_ids),
        active=len(active_ids),
        reported=len(unique_reported),
        missing_active=missing_active,
        unknown=unknown,
    )


def report_display_status(item: dict) -> str:
    """Short status for the admin dashboard and report history."""
    if item.get("report_source") == "manual":
        return "manual"
    if item.get("status") == "verified":
        return "checked"
    reason_keys = {
        reason.get("key")
        for reason in item.get("reasons") or []
        if isinstance(reason, dict)
    }
    if reason_keys & {
        "reason_no_registry",
        "reason_duplicates",
        "reason_missing_active",
        "reason_unknown",
        "reason_partner_discrepancies",
    }:
        return "discrepancies"
    return "attention"


def _reports_unique_includes_application_id(db) -> bool:
    """Check whether the reports table already has application_id in its UNIQUE index."""
    rows = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'reports'"
    ).fetchall()
    for row in rows:
        sql = (row["sql"] or "").upper()
        if "APPLICATION_ID" in sql and "PARTNER_ID" in sql and "PERIOD" in sql:
            return True
    return False


def _migrate_invite_telegram_key(db) -> None:
    """Ensure every partner's notice_settings has an ``invite_telegram`` entry.

    The Telegram invite has its own template key so the dispatch logic can
    resolve it independently from the email ``invite``. Existing partners
    stored before this key was introduced do not have an entry, so the
    dispatcher would always fall back to the i18n default. The migration
    backfills an empty ``message`` for every active and archived partner;
    an empty value means "use the i18n default" and is the explicit signal
    that the partner has not opted into a custom Telegram wording.

    The migration is idempotent: partners that already have the entry are
    left untouched. Stored data outside ``notice_settings.notices`` is
    preserved verbatim.
    """
    from revshare.notifications import default_schedule

    schedule_stub = default_schedule()
    default_invite_telegram = next(
        item for item in schedule_stub["notices"] if item["key"] == "invite_telegram"
    )
    rows = db.execute("SELECT id, notifications FROM partners").fetchall()
    for row in rows:
        raw = row["notifications"] or "{}"
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        notices = parsed.get("notices") if isinstance(parsed, dict) else None
        if not isinstance(notices, list):
            notices = []
        if any(
            isinstance(item, dict) and item.get("key") == "invite_telegram"
            for item in notices
        ):
            continue
        notices.append(
            {
                "key": "invite_telegram",
                "enabled": default_invite_telegram["enabled"],
                "day": default_invite_telegram["day"],
                "audience": default_invite_telegram["audience"],
                "message": "",
            }
        )
        if isinstance(parsed, dict):
            parsed["notices"] = notices
            updated = json.dumps(parsed, ensure_ascii=False)
        else:
            updated = json.dumps(
                {"language": "en", "notices": notices}, ensure_ascii=False
            )
        db.execute(
            "UPDATE partners SET notifications = ? WHERE id = ?",
            (updated, row["id"]),
        )


def _migrate_reports_unique_with_application_id(db) -> None:
    """Recreate the reports table with UNIQUE(partner_id, period, application_id).

    Safe migration:
      1. Build reports_new with the new schema.
      2. Copy rows verbatim.
      3. Drop old table, rename new, recreate indexes.
    """
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS reports_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                application_id INTEGER,
                turnover REAL NOT NULL,
                currency TEXT NOT NULL,
                reported_share REAL,
                client_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                risk_score INTEGER NOT NULL,
                reasons TEXT NOT NULL,
                missing_active TEXT NOT NULL,
                unknown_locations TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                attachment TEXT,
                report_source TEXT NOT NULL DEFAULT 'partner_file',
                submitted_at TEXT NOT NULL,
                workflow_status TEXT NOT NULL DEFAULT 'auto_checked',
                approved_at TEXT,
                approved_by TEXT,
                posted_at TEXT,
                contract_rate REAL,
                application_breakdown TEXT NOT NULL DEFAULT '[]',
                status_updated_at TEXT,
                UNIQUE(partner_id, period, application_id)
            )
            """
        )
        db.execute(
            """
            INSERT INTO reports_new (
                id, partner_id, period, application_id, turnover, currency,
                reported_share, client_count, status, risk_level, risk_score,
                reasons, missing_active, unknown_locations, comment, attachment,
                report_source, submitted_at, workflow_status, approved_at,
                approved_by, posted_at, contract_rate, application_breakdown,
                status_updated_at
            )
            SELECT
                id, partner_id, period, application_id, turnover, currency,
                reported_share, client_count, status, risk_level, risk_score,
                reasons, missing_active, unknown_locations, comment, attachment,
                report_source, submitted_at, workflow_status, approved_at,
                approved_by, posted_at, contract_rate, application_breakdown,
                status_updated_at
            FROM reports
            """
        )
        db.execute("DROP TABLE reports")
        db.execute("ALTER TABLE reports_new RENAME TO reports")
    finally:
        db.execute("PRAGMA foreign_keys = ON")


class Store:
    def __init__(
        self,
        path: Path | None = None,
        fx_loader: Callable[[str], dict[str, float] | None] | None = None,
    ):
        self.path = path or DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fx_loader = fx_loader
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS partners (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    legal_name TEXT NOT NULL DEFAULT '',
                    legal_country TEXT NOT NULL DEFAULT '',
                    altegio_legal_entity TEXT NOT NULL DEFAULT '',
                    contact_name TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    telegram TEXT NOT NULL DEFAULT '',
                    report_channel TEXT NOT NULL DEFAULT 'email',
                    internal_comment TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL UNIQUE,
                    rate REAL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    active INTEGER NOT NULL DEFAULT 1,
                    notifications TEXT NOT NULL DEFAULT '{}',
                    report_automation_status TEXT NOT NULL DEFAULT 'manual'
                );
                CREATE TABLE IF NOT EXISTS usage_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    is_connected INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    api_events INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(partner_id, period, location_id)
                );
                CREATE TABLE IF NOT EXISTS usage_application_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    application_id INTEGER NOT NULL,
                    location_id TEXT NOT NULL,
                    is_connected INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    had_payment INTEGER NOT NULL DEFAULT 0,
                    location_name TEXT NOT NULL DEFAULT '',
                    connected_at TEXT NOT NULL DEFAULT '',
                    last_api_at TEXT NOT NULL DEFAULT '',
                    has_api_amounts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(partner_id, period, application_id, location_id)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    turnover REAL NOT NULL,
                    currency TEXT NOT NULL,
                    reported_share REAL,
                    client_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    reasons TEXT NOT NULL,
                    missing_active TEXT NOT NULL,
                    unknown_locations TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    attachment TEXT,
                    report_source TEXT NOT NULL DEFAULT 'partner_file',
                    submitted_at TEXT NOT NULL,
                    application_breakdown TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(partner_id, period)
                );
                CREATE TABLE IF NOT EXISTS report_clients (
                    report_id INTEGER NOT NULL,
                    location_id TEXT NOT NULL,
                    UNIQUE(report_id, location_id)
                );
                CREATE TABLE IF NOT EXISTS partner_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    UNIQUE(partner_id, url)
                );
                CREATE TABLE IF NOT EXISTS report_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    partner_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    attachment TEXT NOT NULL,
                    turnover REAL,
                    currency TEXT,
                    reported_share REAL,
                    location_ids TEXT NOT NULL,
                    extraction_notes TEXT NOT NULL,
                    application_breakdown TEXT NOT NULL DEFAULT '[]',
                    application_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_claims (
                    report_id INTEGER NOT NULL,
                    location_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL,
                    verification_evidence TEXT NOT NULL,
                    UNIQUE(report_id, location_id, kind)
                );
                CREATE TABLE IF NOT EXISTS report_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    turnover REAL NOT NULL,
                    currency TEXT NOT NULL,
                    reported_share REAL,
                    contract_rate REAL,
                    client_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    reasons TEXT NOT NULL,
                    missing_active TEXT NOT NULL DEFAULT '[]',
                    unknown_locations TEXT NOT NULL DEFAULT '[]',
                    claims TEXT NOT NULL DEFAULT '[]',
                    comment TEXT NOT NULL DEFAULT '',
                    attachment TEXT,
                    submitted_at TEXT NOT NULL,
                    application_breakdown TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(report_id, version)
                );
                """
            )
            report_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(reports)").fetchall()
            }
            if "workflow_status" not in report_columns:
                db.execute(
                    """
                    ALTER TABLE reports
                    ADD COLUMN workflow_status TEXT NOT NULL DEFAULT 'auto_checked'
                    """
                )
            if "report_source" not in report_columns:
                db.execute(
                    "ALTER TABLE reports ADD COLUMN report_source TEXT NOT NULL DEFAULT 'partner_file'"
                )
            if "approved_at" not in report_columns:
                db.execute("ALTER TABLE reports ADD COLUMN approved_at TEXT")
            if "approved_by" not in report_columns:
                db.execute("ALTER TABLE reports ADD COLUMN approved_by TEXT")
            if "posted_at" not in report_columns:
                db.execute("ALTER TABLE reports ADD COLUMN posted_at TEXT")
            if "status_updated_at" not in report_columns:
                db.execute("ALTER TABLE reports ADD COLUMN status_updated_at TEXT")
                # Backfill: existing rows use submitted_at as their first status timestamp.
                db.execute(
                    """
                    UPDATE reports
                    SET status_updated_at = submitted_at
                    WHERE status_updated_at IS NULL OR status_updated_at = ''
                    """
                )
            if "contract_rate" not in report_columns:
                db.execute("ALTER TABLE reports ADD COLUMN contract_rate REAL")
                db.execute(
                    """
                    UPDATE reports
                    SET contract_rate = (
                        SELECT rate FROM partners WHERE partners.id = reports.partner_id
                    )
                    """
                )
            version_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(report_versions)").fetchall()
            }
            if "contract_rate" not in version_columns:
                db.execute("ALTER TABLE report_versions ADD COLUMN contract_rate REAL")
            if "application_breakdown" not in report_columns:
                db.execute(
                    "ALTER TABLE reports ADD COLUMN application_breakdown TEXT NOT NULL DEFAULT '[]'"
                )
            if "application_id" not in report_columns:
                db.execute(
                    "ALTER TABLE reports ADD COLUMN application_id INTEGER"
                )
            # Migrate UNIQUE(partner_id, period) → UNIQUE(partner_id, period, application_id)
            if not _reports_unique_includes_application_id(db):
                _migrate_reports_unique_with_application_id(db)
            # Backfill invite_telegram entry for every partner's notice_settings.
            # Idempotent: partners that already have the entry are skipped.
            _migrate_invite_telegram_key(db)
            if "application_id" not in {
                row["name"]
                for row in db.execute("PRAGMA table_info(report_drafts)").fetchall()
            }:
                db.execute(
                    "ALTER TABLE report_drafts ADD COLUMN application_id INTEGER"
                )
            # Partial unique index: only one draft per (partner, period, application_id)
            # when application_id is set. NULLs are not constrained (per SQLite design)
            # so partners without an explicit application can still keep one draft.
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_draft_partner_period_app
                ON report_drafts (partner_id, period, application_id)
                WHERE application_id IS NOT NULL
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER,
                    chat_id TEXT,
                    update_id INTEGER,
                    kind TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '',
                    response TEXT NOT NULL DEFAULT '',
                    report_id INTEGER,
                    draft_id INTEGER,
                    period TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            draft_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(report_drafts)").fetchall()
            }
            if "application_breakdown" not in draft_columns:
                db.execute(
                    "ALTER TABLE report_drafts ADD COLUMN application_breakdown TEXT NOT NULL DEFAULT '[]'"
                )
            if "application_breakdown" not in version_columns:
                db.execute(
                    "ALTER TABLE report_versions ADD COLUMN application_breakdown TEXT NOT NULL DEFAULT '[]'"
                )
            if "line_items" not in version_columns:
                db.execute(
                    "ALTER TABLE report_versions ADD COLUMN line_items TEXT NOT NULL DEFAULT '[]'"
                )
            if "missing_active" not in version_columns:
                db.execute(
                    "ALTER TABLE report_versions ADD COLUMN missing_active TEXT NOT NULL DEFAULT '[]'"
                )
            if "unknown_locations" not in version_columns:
                db.execute(
                    "ALTER TABLE report_versions ADD COLUMN unknown_locations TEXT NOT NULL DEFAULT '[]'"
                )
            if "claims" not in version_columns:
                db.execute(
                    "ALTER TABLE report_versions ADD COLUMN claims TEXT NOT NULL DEFAULT '[]'"
                )
            application_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(partner_applications)").fetchall()
            }
            if "comment" not in application_columns:
                db.execute(
                    """
                    ALTER TABLE partner_applications
                    ADD COLUMN comment TEXT NOT NULL DEFAULT ''
                    """
                )
            if "category" not in application_columns:
                db.execute(
                    """
                    ALTER TABLE partner_applications
                    ADD COLUMN category TEXT NOT NULL DEFAULT ''
                    """
                )
            partner_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(partners)").fetchall()
            }
            if "legal_name" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN legal_name TEXT NOT NULL DEFAULT ''"
                )
            if "legal_country" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN legal_country TEXT NOT NULL DEFAULT ''"
                )
            if "altegio_legal_entity" not in partner_columns:
                db.execute(
                    """
                    ALTER TABLE partners
                    ADD COLUMN altegio_legal_entity TEXT NOT NULL DEFAULT ''
                    """
                )
            if "report_channel" not in partner_columns:
                db.execute(
                    """
                    ALTER TABLE partners
                    ADD COLUMN report_channel TEXT NOT NULL DEFAULT 'email'
                    """
                )
            if "notifications" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN notifications TEXT NOT NULL DEFAULT '{}'"
                )
            if "telegram_chat_id" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN telegram_chat_id TEXT NOT NULL DEFAULT ''"
                )
            if "category" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN category TEXT NOT NULL DEFAULT ''"
                )
            if "internal_comment" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN internal_comment TEXT NOT NULL DEFAULT ''"
                )
            if "report_automation_status" not in partner_columns:
                db.execute(
                    "ALTER TABLE partners ADD COLUMN report_automation_status TEXT NOT NULL DEFAULT 'manual'"
                )
            legacy_application_categories = db.execute(
                """
                SELECT a.id, p.category
                FROM partner_applications a
                JOIN partners p ON p.id = a.partner_id
                WHERE a.category = '' AND p.category != ''
                """
            ).fetchall()
            for application in legacy_application_categories:
                db.execute(
                    "UPDATE partner_applications SET category = ? WHERE id = ?",
                    (
                        canonical_app_category(application["category"]),
                        application["id"],
                    ),
                )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notice_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    notice_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    source TEXT NOT NULL,
                    connected_count INTEGER NOT NULL,
                    active_count INTEGER NOT NULL,
                    payment_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    workflow_status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sheet_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    partner_name TEXT NOT NULL,
                    turnover REAL NOT NULL,
                    currency TEXT NOT NULL,
                    expected_share REAL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    sheet_cell TEXT NOT NULL DEFAULT '',
                    posted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_bases (
                    period TEXT PRIMARY KEY,
                    uploaded_at TEXT NOT NULL,
                    uploaded_by TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reconciliation_base_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period TEXT NOT NULL,
                    application_id INTEGER NOT NULL,
                    location_id TEXT NOT NULL,
                    turnover REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    active INTEGER NOT NULL DEFAULT 0,
                    source_row INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(period, application_id, location_id)
                );
                CREATE INDEX IF NOT EXISTS idx_reconciliation_base_rows_period
                    ON reconciliation_base_rows(period);
                CREATE TABLE IF NOT EXISTS revalidation_tasks (
                    id TEXT PRIMARY KEY,
                    period TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    current_report TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_revalidation_tasks_period_status
                    ON revalidation_tasks(period, status);
                """
            )
            usage_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(usage_snapshots)").fetchall()
            }
            if "had_payment" not in usage_columns:
                db.execute(
                    """
                    ALTER TABLE usage_snapshots
                    ADD COLUMN had_payment INTEGER NOT NULL DEFAULT 0
                    """
                )
            for column, definition in (
                ("location_name", "TEXT NOT NULL DEFAULT ''"),
                ("connected_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_api_at", "TEXT NOT NULL DEFAULT ''"),
                ("has_api_amounts", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in usage_columns:
                    db.execute(
                        f"ALTER TABLE usage_snapshots ADD COLUMN {column} {definition}"
                    )
            sheet_write_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(sheet_writes)").fetchall()
            }
            if "sheet_cell" not in sheet_write_columns:
                db.execute(
                    """
                    ALTER TABLE sheet_writes
                    ADD COLUMN sheet_cell TEXT NOT NULL DEFAULT ''
                    """
                )
            count = db.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
            if count == 0:
                db.execute(
                    """
                    INSERT INTO partners
                    (name, contact_name, email, telegram, token, rate, currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Demo Integration Hub App",
                        "Demo Partner",
                        "partner@example.com",
                        "@demo_partner",
                        "demo-partner",
                        0.30,
                        "USD",
                    ),
                )
            db.execute(
                """
                UPDATE partners SET name = 'Demo Integration Hub App'
                WHERE name = 'Demo Marketplace App'
                """
            )
    def partner(self, partner_id: int) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM partners WHERE id = ?", (partner_id,)
            ).fetchone()
        return self._with_notifications(row)

    def partner_by_token(self, token: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM partners WHERE token = ? AND active = 1",
                (token,),
            ).fetchone()
        return self._with_notifications(row)

    def partner_by_chat_id(self, chat_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM partners WHERE telegram_chat_id = ? AND active = 1",
                (str(chat_id),),
            ).fetchone()
        return self._with_notifications(row)

    def partners(self) -> list[dict]:
        with self.connect() as db:
            rows = list(db.execute("SELECT * FROM partners WHERE active = 1 ORDER BY name"))
        return [self._with_notifications(row) for row in rows]

    def archived_partners(self) -> list[dict]:
        with self.connect() as db:
            rows = list(db.execute("SELECT * FROM partners WHERE active = 0 ORDER BY name"))
        return [self._with_notifications(row) for row in rows]

    def restore_partner(self, partner_id: int) -> bool:
        with self.connect() as db:
            result = db.execute(
                "UPDATE partners SET active = 1 WHERE id = ? AND active = 0",
                (partner_id,),
            )
        return result.rowcount > 0

    def _with_notifications(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        raw = item.get("notifications") or "{}"
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        settings = normalize_notice_settings(
            parsed, global_schedule=self.global_schedule()
        )
        templates = self.notice_templates(settings["language"])
        for notice in settings["notices"]:
            if notice["key"] in templates:
                notice["message"] = templates[notice["key"]]
        item["category"] = canonical_app_category(item.get("category"))
        item["notice_settings"] = settings
        return item

    def _parse_rate_map(self, raw: str | dict | None) -> dict[str, float]:
        if not raw:
            return {}
        if isinstance(raw, dict):
            parsed = raw
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        if not isinstance(parsed, dict):
            return {}
        rates = {}
        for code, value in parsed.items():
            currency = str(code).upper()
            if currency not in SUPPORTED_CURRENCIES:
                continue
            try:
                rate = float(value)
            except (TypeError, ValueError):
                continue
            if rate > 0:
                rates[currency] = rate
        return rates

    def _manual_fx_rates(self) -> dict[str, float]:
        rates = {}
        with self.connect() as db:
            rows = db.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE 'fx_rate_%'"
            ).fetchall()
        for row in rows:
            code = row["key"].removeprefix("fx_rate_").upper()
            try:
                value = float(row["value"])
            except (TypeError, ValueError):
                continue
            if code in SUPPORTED_CURRENCIES and value > 0:
                rates[code] = value
        return rates

    def fx_rates(self) -> dict[str, float]:
        rates = dict(DEFAULT_USD_RATES)
        rates.update(self._manual_fx_rates())
        return rates

    def fx_snapshot(self, period: str, *, refresh: bool = False) -> dict:
        period = valid_period(period) or ""
        rates = dict(DEFAULT_USD_RATES)
        rates.update(self._manual_fx_rates())
        info = {
            "period": period,
            "rates": rates,
            "source": "fallback",
            "days": 0,
            "overridden": False,
            "fetched_at": "",
        }
        if not period:
            return info
        override = self._parse_rate_map(self.setting(f"fx_override_{period}"))
        cached_raw = self.setting(f"fx_month_{period}")
        cached = {}
        if cached_raw:
            try:
                cached = json.loads(cached_raw)
            except json.JSONDecodeError:
                cached = {}
        if not isinstance(cached, dict):
            cached = {}
        if override and not refresh:
            rates.update(override)
            info.update(
                {
                    "rates": rates,
                    "source": "manual",
                    "overridden": True,
                    "days": int(cached.get("days") or 0),
                    "fetched_at": str(cached.get("fetched_at") or ""),
                }
            )
            return info
        if cached.get("rates") and not refresh:
            rates.update(self._parse_rate_map(cached.get("rates")))
            info.update(
                {
                    "rates": rates,
                    "source": str(cached.get("source") or "auto"),
                    "days": int(cached.get("days") or 0),
                    "fetched_at": str(cached.get("fetched_at") or ""),
                }
            )
            return info
        loaded = self.fx_loader(period) if self.fx_loader else None
        if loaded:
            fetched = dict(loaded)
            days = int(fetched.pop("_days", 0) or 0)
            clean = {
                code: value
                for code, value in fetched.items()
                if code in SUPPORTED_CURRENCIES
            }
            payload = {
                "period": period,
                "source": "auto",
                "days": days,
                "rates": clean,
                "fetched_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
            self.set_setting(f"fx_month_{period}", json.dumps(payload, ensure_ascii=False))
            if refresh:
                self.set_setting(f"fx_override_{period}", "")
            rates.update(clean)
            info.update(
                {
                    "rates": rates,
                    "source": "auto",
                    "days": days,
                    "overridden": False,
                    "fetched_at": payload["fetched_at"],
                }
            )
            return info
        info["rates"] = rates
        return info

    def fx_rates_for(self, period: str, *, refresh: bool = False) -> dict[str, float]:
        return self.fx_snapshot(period, refresh=refresh)["rates"]

    def autoload_reporting_rates(self, today: date | None = None) -> bool:
        """Load the closed reporting month once, typically on the 1st."""
        if not self.fx_loader:
            return False
        today = today or local_today()
        period = reporting_period(today)
        if self.setting("fx_auto_for") == period:
            return False
        if self._parse_rate_map(self.setting(f"fx_override_{period}")):
            self.set_setting("fx_auto_for", period)
            return False
        snapshot = self.fx_snapshot(period, refresh=True)
        if snapshot["source"] != "auto":
            return False
        self.set_setting("fx_auto_for", period)
        return True

    def set_fx_rates(self, rates: dict[str, str], period: str | None = None) -> None:
        parsed: dict[str, float] = {}
        for code, raw in rates.items():
            currency = code.upper()
            if currency not in SUPPORTED_CURRENCIES or currency == "USD":
                continue
            text = (raw or "").replace(",", ".").strip()
            if not text:
                self.set_setting(f"fx_rate_{currency}", "")
                continue
            try:
                value = float(text)
            except ValueError:
                continue
            if value > 0:
                self.set_setting(f"fx_rate_{currency}", str(value))
                parsed[currency] = value
        checked = valid_period(period)
        if checked and parsed:
            self.set_setting(
                f"fx_override_{checked}",
                json.dumps(parsed, ensure_ascii=False),
            )

    def global_schedule(self) -> dict:
        raw = self.setting("notice_schedule")
        if not raw:
            return normalize_schedule(None)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        return normalize_schedule(parsed if isinstance(parsed, dict) else {})

    def set_global_schedule(
        self, enabled_keys: list[str], days: dict[str, str]
    ) -> None:
        notices = []
        enabled = set(enabled_keys)
        current = {item["key"]: item for item in self.global_schedule()["notices"]}
        for key in NOTICE_KEYS:
            raw_day = days.get(key, str(current[key]["day"]))
            try:
                day = int(raw_day)
            except ValueError:
                day = current[key]["day"]
            notices.append(
                {
                    "key": key,
                    "enabled": key in enabled,
                    "day": min(28, max(1, day)),
                    "audience": "internal" if key == "digest" else "partner",
                }
            )
        self.set_setting(
            "notice_schedule",
            json.dumps({"weekend_shift": True, "notices": notices}, ensure_ascii=False),
        )

    def notice_templates(self, language: str) -> dict[str, str]:
        language = notice_language(language)
        stored = {}
        with self.connect() as db:
            rows = db.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE ?",
                (f"notice_text_{language}_%",),
            ).fetchall()
        for row in rows:
            key = row["key"].removeprefix(f"notice_text_{language}_")
            if key in PARTNER_NOTICE_KEYS and row["value"].strip():
                stored[key] = row["value"]
        return {
            key: stored.get(key) or default_message(key, language)
            for key in PARTNER_NOTICE_KEYS
        }

    def set_notice_templates(self, language: str, messages: dict[str, str]) -> None:
        language = notice_language(language)
        for key in PARTNER_NOTICE_KEYS:
            if key not in messages:
                continue
            text = (messages[key] or "").strip()
            default = default_message(key, language)
            self.set_setting(
                f"notice_text_{language}_{key}", "" if text == default else text
            )

    def application_links(self, partner_id: int) -> list[dict[str, str]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT url, comment, category FROM partner_applications
                WHERE partner_id = ? ORDER BY id
                """,
                (partner_id,),
            ).fetchall()
        return [
            {
                "url": row["url"],
                "comment": row["comment"],
                "category": canonical_app_category(row["category"]),
            }
            for row in rows
        ]

    def replace_application_links(
        self, partner_id: int, links: list[dict[str, str]]
    ) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM partner_applications WHERE partner_id = ?",
                (partner_id,),
            )
            db.executemany(
                """
                INSERT INTO partner_applications (partner_id, url, comment, category)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        partner_id,
                        item["url"],
                        item["comment"],
                        canonical_app_category(item.get("category")),
                    )
                    for item in links
                ],
            )

    def add_partner(
        self,
        name: str,
        legal_name: str,
        legal_country: str,
        altegio_legal_entity: str,
        contact_name: str,
        email: str,
        telegram: str,
        report_channel: str,
        token: str,
        rate: float | None,
        currency: str,
        notice_settings: dict | None = None,
        category: str = "",
        internal_comment: str = "",
        report_automation_status: str = "manual",
    ) -> int:
        settings = json.dumps(
            normalize_notice_settings(
                notice_settings, global_schedule=self.global_schedule()
            ),
            ensure_ascii=False,
        )
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO partners
                (name, legal_name, legal_country, altegio_legal_entity,
                contact_name, email, telegram, report_channel, token, rate,
                currency, notifications, category, internal_comment,
                report_automation_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    legal_name,
                    legal_country,
                    altegio_legal_entity,
                    contact_name,
                    email,
                    telegram,
                    report_channel,
                    token,
                    rate,
                    currency,
                    settings,
                    category,
                    internal_comment,
                    report_automation_status,
                ),
            )
        return int(cursor.lastrowid)

    def update_partner(
        self,
        partner_id: int,
        name: str,
        legal_name: str,
        legal_country: str,
        altegio_legal_entity: str,
        contact_name: str,
        email: str,
        telegram: str,
        report_channel: str,
        rate: float | None,
        currency: str,
        notice_settings: dict | None = None,
        category: str = "",
        internal_comment: str = "",
        report_automation_status: str | None = None,
    ) -> None:
        assignments = """
                    name = ?,
                    legal_name = ?,
                    legal_country = ?,
                    altegio_legal_entity = ?,
                    contact_name = ?,
                    email = ?,
                    telegram = ?,
                    report_channel = ?,
                    rate = ?,
                    currency = ?,
                    category = ?,
                    internal_comment = ?
        """
        values: list[object] = [
            name,
            legal_name,
            legal_country,
            altegio_legal_entity,
            contact_name,
            email,
            telegram,
            report_channel,
            rate,
            currency,
            category,
            internal_comment,
        ]
        if report_automation_status is not None:
            assignments += ",\n                    report_automation_status = ?"
            values.append(report_automation_status)
        if notice_settings is not None:
            assignments += ",\n                    notifications = ?"
            values.append(
                json.dumps(
                    normalize_notice_settings(
                        notice_settings, global_schedule=self.global_schedule()
                    ),
                    ensure_ascii=False,
                )
            )
        values.append(partner_id)
        with self.connect() as db:
            db.execute(
                f"UPDATE partners SET {assignments} WHERE id = ? AND active = 1",
                values,
            )

    def set_partner_notifications(self, partner_id: int, settings: dict) -> None:
        payload = json.dumps(
            normalize_notice_settings(
                settings, global_schedule=self.global_schedule()
            ),
            ensure_ascii=False,
        )
        with self.connect() as db:
            db.execute(
                "UPDATE partners SET notifications = ? WHERE id = ? AND active = 1",
                (payload, partner_id),
            )

    def deactivate_partner(self, partner_id: int) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT name FROM partners WHERE id = ? AND active = 1",
                (partner_id,),
            ).fetchone()
            if not row:
                return False
            db.execute(
                """
                UPDATE partners
                SET active = 0, token = ?, name = ?
                WHERE id = ? AND active = 1
                """,
                (
                    f"deleted-{partner_id}-{secrets.token_hex(8)}",
                    f"{row['name']} (deleted {partner_id})",
                    partner_id,
                ),
            )
        return True

    def usage(self, partner_id: int, period: str) -> tuple[set[str], set[str]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_id, is_connected, is_active
                FROM usage_snapshots WHERE partner_id = ? AND period = ?
                """,
                (partner_id, period),
            ).fetchall()
        connected = {row["location_id"] for row in rows if row["is_connected"]}
        active = {row["location_id"] for row in rows if row["is_active"]}
        return connected, active

    def usage_evidence(self, partner_id: int, period: str) -> dict[str, dict[str, bool]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_id, is_connected, is_active, had_payment
                FROM usage_snapshots WHERE partner_id = ? AND period = ?
                """,
                (partner_id, period),
            ).fetchall()
        return {
            row["location_id"]: {
                "is_connected": bool(row["is_connected"]),
                "is_active": bool(row["is_active"]),
                "had_payment": bool(row["had_payment"]),
            }
            for row in rows
        }

    def idle_months_threshold(self) -> int:
        try:
            value = int(self.setting("idle_months_threshold", "3"))
        except ValueError:
            value = 3
        return min(12, max(2, value))

    def set_idle_months_threshold(self, value: str | int) -> None:
        try:
            months = int(value)
        except (TypeError, ValueError):
            months = 3
        self.set_setting("idle_months_threshold", str(min(12, max(2, months))))

    def disconnect_candidates(
        self,
        period: str,
        threshold: int | None = None,
        partner_id: int | None = None,
    ) -> list[dict]:
        """Locations idle and without payment callbacks for consecutive months."""
        minimum = threshold or self.idle_months_threshold()
        query = """
            SELECT u.partner_id, p.name AS partner_name, u.period,
            u.location_id, u.is_connected, u.is_active, u.had_payment
            FROM usage_snapshots u
            JOIN partners p ON p.id = u.partner_id
            WHERE p.active = 1 AND u.period <= ?
        """
        values: list[object] = [period]
        if partner_id is not None:
            query += " AND u.partner_id = ?"
            values.append(partner_id)
        query += " ORDER BY u.partner_id, u.location_id, u.period DESC"
        with self.connect() as db:
            rows = db.execute(query, values).fetchall()
        evidence = {
            (row["partner_id"], row["location_id"], row["period"]): row for row in rows
        }
        current = [
            row
            for row in rows
            if row["period"] == period
            and row["is_connected"]
            and not row["is_active"]
            and not row["had_payment"]
        ]
        candidates = []
        for row in current:
            streak = 0
            cursor = period
            while streak < 24:
                month = evidence.get(
                    (row["partner_id"], row["location_id"], cursor)
                )
                if (
                    month is None
                    or not month["is_connected"]
                    or month["is_active"]
                    or month["had_payment"]
                ):
                    break
                streak += 1
                cursor = previous_period(cursor)
            if streak >= minimum:
                candidates.append(
                    {
                        "partner_id": row["partner_id"],
                        "partner_name": row["partner_name"],
                        "location_id": row["location_id"],
                        "months": streak,
                    }
                )
        for item in candidates:
            start = period
            for _ in range(item["months"] - 1):
                start = previous_period(start)
            item["since"] = start
        return sorted(
            candidates,
            key=lambda item: (
                -item["months"],
                item["partner_name"].casefold(),
                item["location_id"],
            ),
        )

    def usage_location_details(
        self, partner_id: int, period: str
    ) -> dict[str, dict[str, object]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_id, location_name, connected_at, last_api_at,
                       has_api_amounts
                FROM usage_snapshots
                WHERE partner_id = ? AND period = ?
                """,
                (partner_id, period),
            ).fetchall()
        return {row["location_id"]: dict(row) for row in rows}

    def application_usage(
        self, partner_id: int, period: str, application_id: int
    ) -> tuple[set[str], set[str]] | None:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_id, is_connected, is_active
                FROM usage_application_snapshots
                WHERE partner_id = ? AND period = ? AND application_id = ?
                """,
                (partner_id, period, application_id),
            ).fetchall()
        if not rows:
            return None
        connected = {row["location_id"] for row in rows if row["is_connected"]}
        active = {row["location_id"] for row in rows if row["is_active"]}
        return connected, active

    def application_payment_ids(
        self, partner_id: int, period: str, application_id: int
    ) -> set[str]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_id
                FROM usage_application_snapshots
                WHERE partner_id = ? AND period = ? AND application_id = ?
                  AND had_payment = 1
                """,
                (partner_id, period, application_id),
            ).fetchall()
        return {row["location_id"] for row in rows}

    def replace_application_usage(
        self,
        partner_id: int,
        period: str,
        application_id: int,
        connected_ids: list[str],
        active_ids: list[str],
        payment_ids: list[str] | None = None,
        location_details: dict[str, dict[str, object]] | None = None,
    ) -> None:
        payment_ids = payment_ids or []
        all_ids = set(connected_ids) | set(active_ids) | set(payment_ids)
        details = location_details or {}
        with self.connect() as db:
            db.execute(
                """
                DELETE FROM usage_application_snapshots
                WHERE partner_id = ? AND period = ? AND application_id = ?
                """,
                (partner_id, period, application_id),
            )
            db.executemany(
                """
                INSERT INTO usage_application_snapshots
                (partner_id, period, application_id, location_id, is_connected,
                 is_active, had_payment, location_name, connected_at, last_api_at,
                 has_api_amounts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        partner_id,
                        period,
                        application_id,
                        location_id,
                        int(location_id in connected_ids),
                        int(location_id in active_ids),
                        int(location_id in payment_ids),
                        *normalize_location_detail(details.get(location_id)).values(),
                    )
                    for location_id in sorted(all_ids)
                ],
            )

    def replace_usage(
        self,
        partner_id: int,
        period: str,
        connected_ids: list[str],
        active_ids: list[str],
        payment_ids: list[str] | None = None,
        source: str = "manual",
        location_details: dict[str, dict[str, object]] | None = None,
    ) -> None:
        payment_ids = payment_ids or []
        all_ids = set(connected_ids) | set(active_ids) | set(payment_ids)
        details = location_details or {}
        with self.connect() as db:
            db.execute(
                "DELETE FROM usage_snapshots WHERE partner_id = ? AND period = ?",
                (partner_id, period),
            )
            db.executemany(
                """
                INSERT INTO usage_snapshots
                (partner_id, period, location_id, is_connected, is_active, had_payment,
                 location_name, connected_at, last_api_at, has_api_amounts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        partner_id,
                        period,
                        location_id,
                        int(location_id in connected_ids),
                        int(location_id in active_ids),
                        int(location_id in payment_ids),
                        *normalize_location_detail(details.get(location_id)).values(),
                    )
                    for location_id in sorted(all_ids)
                ],
            )
            db.execute(
                """
                INSERT INTO usage_imports
                (partner_id, period, source, connected_count, active_count,
                 payment_count, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    partner_id,
                    period,
                    source,
                    len(connected_ids),
                    len(active_ids),
                    len(payment_ids),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._reassess_report(db, partner_id, period)

    def usage_import(self, partner_id: int, period: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT source, connected_count, active_count, payment_count, imported_at
                FROM usage_imports
                WHERE partner_id = ? AND period = ?
                ORDER BY id DESC LIMIT 1
                """,
                (partner_id, period),
            ).fetchone()
        return dict(row) if row else None

    def partner_history(self, partner_id: int, limit: int = 24) -> list[dict]:
        with self.connect() as db:
            rate_row = db.execute(
                "SELECT rate FROM partners WHERE id = ?", (partner_id,)
            ).fetchone()
            rows = db.execute(
                """
                SELECT id AS report_id, period, turnover, currency, reported_share,
                contract_rate,
                client_count, status, risk_level, risk_score, submitted_at,
                report_source, reasons
                FROM reports WHERE partner_id = ?
                ORDER BY period DESC LIMIT ?
                """,
                (partner_id, limit),
            ).fetchall()
        rate = rate_row["rate"] if rate_row else None
        history = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item["reasons"] or "[]")
            item["display_status"] = report_display_status(item)
            item["expected_share"] = (
                round(
                    item["turnover"]
                    * (
                        item["contract_rate"]
                        if item["contract_rate"] is not None
                        else rate
                    ),
                    2,
                )
                if item["contract_rate"] is not None or rate is not None
                else None
            )
            rates = self.fx_rates_for(item["period"])
            item["turnover_usd"] = to_usd(item["turnover"], item["currency"], rates)
            item["share_usd"] = to_usd(item["expected_share"], item["currency"], rates)
            connected, active = self.usage(partner_id, item["period"])
            item["connected_count"] = len(connected)
            item["active_count"] = len(active)
            history.append(item)
        return history

    def report_id_for(self, partner_id: int, period: str) -> int | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM reports WHERE partner_id = ? AND period = ?",
                (partner_id, period),
            ).fetchone()
        return int(row["id"]) if row else None

    def _log_report_event(
        self,
        db: sqlite3.Connection,
        report_id: int,
        workflow_status: str,
        note: str,
        actor: str = "",
        created_at: str | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO report_events
            (report_id, workflow_status, note, actor, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                report_id,
                workflow_status,
                note,
                actor,
                created_at or datetime.now(UTC).isoformat(),
            ),
        )

    def _reassess_report(
        self, db: sqlite3.Connection, partner_id: int, period: str
    ) -> None:
        report = db.execute(
            """
                   SELECT r.id, r.turnover, r.reported_share, r.workflow_status,
                          r.report_source,
                   COALESCE(r.contract_rate, p.rate) AS rate
            FROM reports r
            JOIN partners p ON p.id = r.partner_id
            WHERE r.partner_id = ? AND r.period = ?
            """,
            (partner_id, period),
        ).fetchone()
        if not report or report["report_source"] == "manual":
            return
        report_id = report["id"]
        # Step 3: if no reconciliation base has been uploaded for this period
        # yet, park the report in ``pending_base`` with empty assessment
        # fields — mirrors ``submit_report`` so the partner sees the same
        # "waiting for base" branch whether the report was just created or
        # is being recomputed by the revalidation worker.
        base_rows = db.execute(
            """
            SELECT location_id, active FROM reconciliation_base_rows
            WHERE period = ? ORDER BY location_id
            """,
            (period,),
        ).fetchall()
        base_meta = db.execute(
            "SELECT 1 FROM reconciliation_bases WHERE period = ?",
            (period,),
        ).fetchone()
        if not base_meta or not base_rows:
            now = datetime.now(UTC).isoformat()
            db.execute(
                """
                UPDATE reports
                SET status = 'pending_base',
                    risk_level = 'green',
                    risk_score = 0,
                    reasons = '[]',
                    missing_active = '[]',
                    unknown_locations = '[]',
                    approved_at = NULL, approved_by = NULL,
                    workflow_status = CASE
                        WHEN workflow_status = 'approved' THEN 'approved'
                        ELSE 'needs_review'
                    END,
                    status_updated_at = ?
                WHERE id = ?
                """,
                (now, report_id),
            )
            if report["workflow_status"] not in ("needs_review", "approved"):
                self._log_report_event(
                    db,
                    report_id,
                    "needs_review",
                    "snapshot_recheck",
                    actor="system",
                )
            return
        location_ids = [
            row["location_id"]
            for row in db.execute(
                "SELECT location_id FROM report_clients WHERE report_id = ?",
                (report_id,),
            )
        ]
        connected = {row["location_id"] for row in base_rows}
        active = {row["location_id"] for row in base_rows if int(row["active"])}
        # ``evidence`` below still keys off ``usage_snapshots`` because the
        # ``verify_claim`` helper only needs the per-location booleans —
        # the base rows tell us which ids are connected/active and the
        # snapshot rows still hold the had_payment flag.
        snapshot_rows = db.execute(
            """
            SELECT location_id, is_connected, is_active, had_payment
            FROM usage_snapshots WHERE partner_id = ? AND period = ?
            """,
            (partner_id, period),
        ).fetchall()
        evidence = {
            row["location_id"]: {
                "is_connected": bool(row["is_connected"]),
                "is_active": bool(row["is_active"]),
                "had_payment": bool(row["had_payment"]),
            }
            for row in snapshot_rows
        }
        previous = db.execute(
            """
            SELECT turnover, client_count FROM reports
            WHERE partner_id = ? AND period < ?
            ORDER BY period DESC LIMIT 1
            """,
            (partner_id, period),
        ).fetchone()
        review = assess(
            location_ids,
            connected,
            active,
            turnover=report["turnover"],
            reported_share=report["reported_share"],
            contract_rate=report["rate"],
            previous_turnover=previous["turnover"] if previous else None,
            previous_client_count=previous["client_count"] if previous else None,
        )
        status = "requires_review" if review.level != "green" else "verified"
        now = datetime.now(UTC).isoformat()
        db.execute(
            """
            UPDATE reports
            SET status = ?, risk_level = ?, risk_score = ?, reasons = ?,
                missing_active = ?, unknown_locations = ?,
                workflow_status = CASE
                    WHEN ? = 'requires_review' THEN 'needs_review'
                    ELSE workflow_status
                END,
                approved_at = CASE
                    WHEN ? = 'requires_review' THEN NULL
                    ELSE approved_at
                END,
                approved_by = CASE
                    WHEN ? = 'requires_review' THEN NULL
                    ELSE approved_by
                END,
                status_updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                review.level,
                review.score,
                json.dumps(review.reasons, ensure_ascii=False),
                json.dumps(review.missing_active),
                json.dumps(review.unknown),
                status,
                status,
                status,
                now,
                report_id,
            ),
        )
        new_workflow = (
            "needs_review" if status == "requires_review" else report["workflow_status"]
        )
        if new_workflow != report["workflow_status"]:
            self._log_report_event(
                db,
                report_id,
                new_workflow,
                "snapshot_recheck",
                actor="system",
            )
        # A partner report is reconciled against locations that were active
        # during the reporting month. Connected locations without activity
        # remain available in the Altegio snapshot, but do not create a claim.
        items = discrepancy_items(location_ids, active, active)
        existing = {
            (row["location_id"], row["kind"]): row
            for row in db.execute(
                """
                SELECT rowid AS claim_rowid, location_id, kind, reason, detail
                FROM report_claims WHERE report_id = ?
                """,
                (report_id,),
            )
        }
        keep = set()
        for item in items:
            key = (item["location_id"], item["kind"])
            facts = evidence.get(item["location_id"], {})
            claim = existing.get(key)
            if item["kind"] == "connected_not_active" and (
                not claim or claim["reason"] == "unexplained"
            ):
                continue
            keep.add(key)
            reason = claim["reason"] if claim else "unexplained"
            detail = claim["detail"] if claim else ""
            status, proof = verify_claim(
                item["kind"],
                reason,
                bool(facts.get("is_connected")),
                bool(facts.get("is_active")),
                bool(facts.get("had_payment")),
            )
            if claim:
                db.execute(
                    """
                    UPDATE report_claims
                    SET verification_status = ?, verification_evidence = ?
                    WHERE rowid = ?
                    """,
                    (status, proof, claim["claim_rowid"]),
                )
            else:
                db.execute(
                    """
                    INSERT INTO report_claims
                    (report_id, location_id, kind, reason, detail,
                    verification_status, verification_evidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        item["location_id"],
                        item["kind"],
                        reason,
                        detail,
                        status,
                        proof,
                    ),
                )
        for key, claim in existing.items():
            if key not in keep:
                db.execute(
                    "DELETE FROM report_claims WHERE rowid = ?",
                    (claim["claim_rowid"],),
                )

    def _apply_cross_partner_conflicts(
        self, db: sqlite3.Connection, period: str
    ) -> None:
        conflicts = db.execute(
            """
            SELECT rc.location_id, GROUP_CONCAT(DISTINCT r.partner_id) AS partner_ids
            FROM report_clients rc
            JOIN reports r ON r.id = rc.report_id
            WHERE r.period = ?
            GROUP BY rc.location_id
            HAVING COUNT(DISTINCT r.partner_id) > 1
            """,
            (period,),
        ).fetchall()
        by_partner: dict[int, list[str]] = {}
        for conflict in conflicts:
            for partner_id in str(conflict["partner_ids"]).split(","):
                by_partner.setdefault(int(partner_id), []).append(conflict["location_id"])
        reports = db.execute(
            """
            SELECT id, partner_id, reasons, risk_score, workflow_status
            FROM reports WHERE period = ?
            """,
            (period,),
        ).fetchall()
        for report in reports:
            reasons = [
                reason
                for reason in json.loads(report["reasons"])
                if reason.get("key") != "reason_cross_partner"
            ]
            location_ids = sorted(by_partner.get(report["partner_id"], []))
            if not location_ids:
                continue
            reasons.append(
                {
                    "key": "reason_cross_partner",
                    "count": len(location_ids),
                    "locations": location_ids,
                }
            )
            score = min(100, int(report["risk_score"]) + 30)
            level = "red" if score >= 50 else "yellow"
            db.execute(
                """
                UPDATE reports
                SET status = 'requires_review', workflow_status = 'needs_review',
                    risk_level = ?, risk_score = ?, reasons = ?,
                    approved_at = NULL, approved_by = NULL,
                    status_updated_at = ?
                WHERE id = ?
                """,
                (
                    level,
                    score,
                    json.dumps(reasons, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                    report["id"],
                ),
            )
            if report["workflow_status"] != "needs_review":
                self._log_report_event(
                    db,
                    report["id"],
                    "needs_review",
                    "cross_partner",
                    actor="system",
                )

    def create_report_draft(
        self,
        partner_id: int,
        period: str,
        attachment: str,
        turnover: float | None,
        currency: str | None,
        reported_share: float | None,
        location_ids: list[str],
        extraction_notes: list[str],
        application_breakdown: list[dict[str, object]] | None = None,
    ) -> str:
        token = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO report_drafts
                (token, partner_id, period, attachment, turnover, currency,
                reported_share, location_ids, extraction_notes, application_breakdown,
                created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    partner_id,
                    period,
                    attachment,
                    turnover,
                    currency,
                    reported_share,
                    json.dumps(location_ids),
                    json.dumps(extraction_notes),
                    json.dumps(application_breakdown or [], ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return token

    def update_report_draft(
        self,
        token: str,
        partner_id: int,
        turnover: float | None,
        currency: str | None,
        reported_share: float | None,
        location_ids: list[str],
        extraction_notes: list[str],
        application_breakdown: list[dict[str, object]] | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE report_drafts
                SET turnover = ?, currency = ?, reported_share = ?,
                    location_ids = ?, extraction_notes = ?, application_breakdown = ?
                WHERE token = ? AND partner_id = ?
                """,
                (
                    turnover,
                    currency,
                    reported_share,
                    json.dumps(location_ids),
                    json.dumps(extraction_notes),
                    json.dumps(application_breakdown or [], ensure_ascii=False),
                    token,
                    partner_id,
                ),
            )

    def report_draft(self, token: str, partner_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM report_drafts
                WHERE token = ? AND partner_id = ?
                """,
                (token, partner_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["location_ids"] = json.loads(result["location_ids"])
        result["extraction_notes"] = json.loads(result["extraction_notes"])
        result["application_breakdown"] = json.loads(
            result.get("application_breakdown") or "[]"
        )
        return result

    def report_draft_by_id(self, draft_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM report_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["location_ids"] = json.loads(result["location_ids"])
        result["extraction_notes"] = json.loads(result["extraction_notes"])
        result["application_breakdown"] = json.loads(
            result.get("application_breakdown") or "[]"
        )
        return result

    def delete_report_draft(self, token: str, partner_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM report_drafts WHERE token = ? AND partner_id = ?",
                (token, partner_id),
            )

    # ---- Bot helpers ----

    def save_bot_draft(
        self,
        partner_id: int,
        period: str,
        application_id: int | None,
        attachment: str,
        turnover: float | None,
        currency: str,
        reported_share: float | None,
        location_ids: list[str],
        extraction_notes: list[str],
        application_breakdown: list[dict[str, object]],
    ) -> int:
        """Insert or replace a draft created from a Telegram bot submission."""
        import secrets as _secrets

        with self.connect() as db:
            if application_id is None:
                db.execute(
                    "DELETE FROM report_drafts WHERE partner_id = ? AND period = ? AND application_id IS NULL",
                    (partner_id, period),
                )
            else:
                db.execute(
                    "DELETE FROM report_drafts WHERE partner_id = ? AND period = ? AND application_id = ?",
                    (partner_id, period, application_id),
                )
            token = _secrets.token_urlsafe(24)
            db.execute(
                """
                INSERT INTO report_drafts (
                    token, partner_id, period, attachment, turnover,
                    currency, reported_share, location_ids, extraction_notes,
                    application_breakdown, application_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    partner_id,
                    period,
                    attachment,
                    turnover,
                    currency,
                    reported_share,
                    json.dumps(location_ids),
                    json.dumps(extraction_notes),
                    json.dumps(application_breakdown),
                    application_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = db.execute(
                "SELECT id FROM report_drafts WHERE token = ?", (token,)
            ).fetchone()
        return int(row[0])

    def drafts_for_partner(self, partner_id: int) -> list[dict]:
        """Return all bot-created drafts for a partner, newest first.

        Used by the admin profile page so admins can review and delete
        drafts that the partner has not yet confirmed.
        """
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, period, application_id, attachment, turnover,
                       currency, reported_share, created_at
                FROM report_drafts
                WHERE partner_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                """,
                (partner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_draft(self, draft_id: int, partner_id: int) -> bool:
        """Hard-delete a draft owned by the given partner.

        Returns True if a row was removed, False if the draft did not
        exist or belonged to a different partner. The partner_id check
        prevents an admin from deleting another partner's draft by
        guessing draft ids.
        """
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM report_drafts WHERE id = ? AND partner_id = ?",
                (draft_id, partner_id),
            )
        return cursor.rowcount > 0

    def log_bot_interaction(
        self,
        partner_id: int | None,
        chat_id: str | None,
        update_id: int | None,
        kind: str,
        direction: str,
        payload: dict[str, object] | None = None,
        response: dict[str, object] | None = None,
        report_id: int | None = None,
        draft_id: int | None = None,
        period: str | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO bot_interactions (
                    partner_id, chat_id, update_id, kind, direction,
                    payload, response, report_id, draft_id, period, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    partner_id,
                    chat_id,
                    update_id,
                    kind,
                    direction,
                    json.dumps(payload or {}),
                    json.dumps(response or {}),
                    report_id,
                    draft_id,
                    period,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def recent_resend_count(self, partner_id: int, period: str) -> int:
        """Count resend events in the current calendar day (UTC) for a partner/period."""
        from datetime import datetime, time

        start_of_day = datetime.combine(datetime.now(UTC).date(), time.min).isoformat()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) FROM bot_interactions
                WHERE partner_id = ?
                  AND period = ?
                  AND kind = 'resend'
                  AND created_at >= ?
                """,
                (partner_id, period, start_of_day),
            ).fetchone()
        return int(row[0])

    def submit_report(
        self,
        partner_id: int,
        period: str,
        turnover: float,
        currency: str,
        reported_share: float | None,
        location_ids: list[str],
        comment: str,
        attachment: str | None,
        claims: list[dict[str, str]] | None = None,
        application_breakdown: list[dict[str, object]] | None = None,
        report_source: str = "partner_file",
        application_id: int | None = None,
    ) -> tuple[int, Review]:
        # Step 3: prefer the new reconciliation base over the legacy Altegio
        # BI snapshot. The base is the authoritative source for ``connected``
        # and ``active`` ids as of this step. If no base has been uploaded
        # yet for the period, fall back to the legacy ``usage_snapshots``
        # reads (kept read-only until step 4 turns the sync into a no-op).
        base_rows = self.get_reconciliation_base_rows(period)
        base_meta = self.get_reconciliation_base(period)
        base_available = bool(base_meta and base_rows)
        if base_available:
            connected = {str(row["location_id"]) for row in base_rows}
            active = {
                str(row["location_id"]) for row in base_rows if int(row["active"])
            }
        else:
            connected, active = self.usage(partner_id, period)
        with self.connect() as db:
            partner = db.execute(
                "SELECT rate FROM partners WHERE id = ?", (partner_id,)
            ).fetchone()
            previous = db.execute(
                """
                SELECT turnover, client_count FROM reports
                WHERE partner_id = ? AND period < ?
                ORDER BY period DESC LIMIT 1
                """,
                (partner_id, period),
            ).fetchone()
        if report_source == "manual":
            review = Review(
                level="green",
                score=0,
                reasons=[],
                connected=len(connected),
                active=len(active),
                reported=0,
                missing_active=[],
                unknown=[],
            )
        elif not base_available:
            # Step 3: no reconciliation base for the period → save the
            # report in the ``pending_base`` state with empty assessment
            # fields. The partner sees the "waiting for base" branch on the
            # review screen and the admin gets the same on ``/reports/{id}``.
            review = Review(
                level="green",
                score=0,
                reasons=[],
                connected=len(connected),
                active=len(active),
                reported=len(set(location_ids)),
                missing_active=[],
                unknown=[],
            )
        else:
            review = assess(
                location_ids,
                connected,
                active,
                turnover=turnover,
                reported_share=reported_share,
                contract_rate=partner["rate"] if partner else None,
                previous_turnover=previous["turnover"] if previous else None,
                previous_client_count=previous["client_count"] if previous else None,
            )
        if report_source != "manual" and claims:
            review.reasons = [
                reason for reason in review.reasons if reason.get("key") != "reason_clear"
            ]
            review.reasons.append(
                {"key": "reason_partner_discrepancies", "count": len(claims)}
            )
            review.score = max(review.score, 1)
            if review.level == "green":
                review.level = "yellow"
        # Step 3: ``pending_base`` status overrides the usual
        # ``requires_review``/``verified`` split — the report is parked
        # waiting for the base to arrive.
        if report_source != "manual" and not base_available:
            status = "pending_base"
            workflow_status = "needs_review"
        else:
            status = "requires_review" if review.level != "green" else "verified"
            workflow_status = "needs_review" if review.level != "green" else "auto_checked"
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            existing = db.execute(
                "SELECT id, application_id FROM reports WHERE partner_id = ? AND period = ?",
                (partner_id, period),
            ).fetchone()
            if existing and existing["application_id"] == application_id:
                report_id = existing["id"]
                db.execute("DELETE FROM report_clients WHERE report_id = ?", (report_id,))
                db.execute(
                    """
                    UPDATE reports SET turnover = ?, currency = ?, reported_share = ?,
                    contract_rate = ?, client_count = ?, status = ?, risk_level = ?, risk_score = ?,
                    reasons = ?, missing_active = ?, unknown_locations = ?,
                    comment = ?, attachment = ?, report_source = ?, submitted_at = ?,
                    workflow_status = ?, application_breakdown = ?,
                    approved_at = NULL, approved_by = NULL, status_updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        turnover,
                        currency,
                        reported_share,
                        partner["rate"] if partner else None,
                        len(set(location_ids)),
                        status,
                        review.level,
                        review.score,
                        json.dumps(review.reasons, ensure_ascii=False),
                        json.dumps(review.missing_active),
                        json.dumps(review.unknown),
                        comment,
                        attachment,
                        report_source,
                        now,
                        workflow_status,
                        json.dumps(application_breakdown or [], ensure_ascii=False),
                        now,
                        report_id,
                    ),
                )
            elif existing:
                # Different application_id for the same (partner, period):
                # the original UNIQUE(partner_id, period, application_id) — keep both rows.
                cursor = db.execute(
                    """
                    INSERT INTO reports
                    (partner_id, period, application_id, turnover, currency, reported_share, contract_rate,
                    client_count, status, risk_level, risk_score, reasons,
                    missing_active, unknown_locations, comment, attachment, report_source, submitted_at,
                    workflow_status, application_breakdown, status_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        partner_id,
                        period,
                        application_id,
                        turnover,
                        currency,
                        reported_share,
                        partner["rate"] if partner else None,
                        len(set(location_ids)),
                        status,
                        review.level,
                        review.score,
                        json.dumps(review.reasons, ensure_ascii=False),
                        json.dumps(review.missing_active),
                        json.dumps(review.unknown),
                        comment,
                        attachment,
                        report_source,
                        now,
                        workflow_status,
                        json.dumps(application_breakdown or [], ensure_ascii=False),
                        now,
                    ),
                )
                report_id = cursor.lastrowid
            else:
                cursor = db.execute(
                    """
                    INSERT INTO reports
                    (partner_id, period, application_id, turnover, currency, reported_share, contract_rate,
                    client_count, status, risk_level, risk_score, reasons,
                    missing_active, unknown_locations, comment, attachment, report_source, submitted_at,
                    workflow_status, application_breakdown, status_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        partner_id,
                        period,
                        application_id,
                        turnover,
                        currency,
                        reported_share,
                        partner["rate"] if partner else None,
                        len(set(location_ids)),
                        status,
                        review.level,
                        review.score,
                        json.dumps(review.reasons, ensure_ascii=False),
                        json.dumps(review.missing_active),
                        json.dumps(review.unknown),
                        comment,
                        attachment,
                        report_source,
                        now,
                        workflow_status,
                        json.dumps(application_breakdown or [], ensure_ascii=False),
                        now,
                    ),
                )
                report_id = cursor.lastrowid
            db.executemany(
                "INSERT OR IGNORE INTO report_clients (report_id, location_id) VALUES (?, ?)",
                [(report_id, location_id) for location_id in set(location_ids)],
            )
            db.execute("DELETE FROM report_claims WHERE report_id = ?", (report_id,))
            evidence = self.usage_evidence(partner_id, period)
            claim_rows = []
            for claim in claims or []:
                facts = evidence.get(claim["location_id"], {})
                status, proof = verify_claim(
                    claim["kind"],
                    claim["reason"],
                    bool(facts.get("is_connected")),
                    bool(facts.get("is_active")),
                    bool(facts.get("had_payment")),
                )
                claim_rows.append(
                    (
                        report_id,
                        claim["location_id"],
                        claim["kind"],
                        claim["reason"],
                        claim.get("detail", ""),
                        status,
                        proof,
                    )
                )
            db.executemany(
                """
                INSERT INTO report_claims
                (report_id, location_id, kind, reason, detail,
                verification_status, verification_evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                claim_rows,
            )
            next_version = db.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM report_versions WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()[0]
            db.execute(
                """
                INSERT INTO report_versions
                (report_id, version, turnover, currency, reported_share, contract_rate,
                client_count, status, risk_level, risk_score, reasons,
                missing_active, unknown_locations, claims, comment, attachment,
                submitted_at, application_breakdown, line_items)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    next_version,
                    turnover,
                    currency,
                    reported_share,
                    partner["rate"] if partner else None,
                    len(set(location_ids)),
                    status,
                    review.level,
                    review.score,
                    json.dumps(review.reasons, ensure_ascii=False),
                    json.dumps(review.missing_active),
                    json.dumps(review.unknown),
                    json.dumps(claims or [], ensure_ascii=False),
                    comment,
                    attachment,
                    now,
                    json.dumps(application_breakdown or [], ensure_ascii=False),
                    json.dumps(_flatten_line_items(application_breakdown), ensure_ascii=False),
                ),
            )
            self._log_report_event(
                db,
                report_id,
                workflow_status,
                "resubmitted" if existing else "submitted",
                actor="partner",
                created_at=now,
            )
            previous = db.execute(
                """
                SELECT MAX(period) AS period FROM reports
                WHERE partner_id = ? AND period < ?
                """,
                (partner_id, period),
            ).fetchone()
            if previous and previous["period"]:
                prior_claims = db.execute(
                    """
                    SELECT c.rowid, c.location_id
                    FROM report_claims c
                    JOIN reports r ON r.id = c.report_id
                    WHERE r.partner_id = ? AND r.period = ?
                      AND c.reason = 'pay_next_month'
                      AND c.verification_status = 'follow_up'
                    """,
                    (partner_id, previous["period"]),
                ).fetchall()
                current_ids = set(location_ids)
                for prior_claim in prior_claims:
                    appeared = prior_claim["location_id"] in current_ids
                    db.execute(
                        """
                        UPDATE report_claims
                        SET verification_status = ?, verification_evidence = ?
                        WHERE rowid = ?
                        """,
                        (
                            "confirmed" if appeared else "contradicted",
                            "appeared_next_report" if appeared else "missing_next_report",
                            prior_claim["rowid"],
                        ),
                    )
            period_reports = db.execute(
                "SELECT DISTINCT partner_id FROM reports WHERE period = ?", (period,)
            ).fetchall()
            for period_report in period_reports:
                self._reassess_report(db, period_report["partner_id"], period)
            self._apply_cross_partner_conflicts(db, period)
        return int(report_id), review

    def submit_manual_report(
        self,
        partner_id: int,
        period: str,
        turnover: float,
        currency: str,
        reported_share: float | None,
        comment: str,
    ) -> tuple[int, Review]:
        """Save an operator-entered report without a partner file or registry."""
        return self.submit_report(
            partner_id=partner_id,
            period=period,
            turnover=turnover,
            currency=currency,
            reported_share=reported_share,
            location_ids=[],
            comment=comment,
            attachment=None,
            claims=[],
            application_breakdown=[],
            report_source="manual",
        )

    def dashboard(self, period: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.*, r.id AS report_id, r.turnover, r.currency AS report_currency,
                r.reported_share, r.contract_rate, r.client_count, r.status, r.risk_level,
                r.risk_score, r.reasons, r.submitted_at, r.workflow_status, r.report_source,
                r.status_updated_at
                FROM partners p
                LEFT JOIN reports r ON r.partner_id = p.id AND r.period = ?
                WHERE p.active = 1 ORDER BY p.name
                """,
                (period,),
            ).fetchall()
        result = []
        rates = self.fx_rates_for(period)
        for row in rows:
            item = dict(row)
            item["category"] = canonical_app_category(item.get("category"))
            if not item["category"]:
                links = self.application_links(item["id"])
                for link in links:
                    item["category"] = link.get("category") or ""
                    if item["category"]:
                        break
            if item["report_id"] and item["contract_rate"] is not None:
                item["rate"] = item["contract_rate"]
            item["reasons"] = json.loads(item["reasons"]) if item["reasons"] else []
            item["display_status"] = (
                "missing"
                if not item["report_id"]
                else report_display_status(item)
            )
            connected, active = self.usage(item["id"], period)
            item["connected_count"] = len(connected)
            item["active_count"] = len(active)
            item["expected_share"] = (
                round(item["turnover"] * item["rate"], 2)
                if item["report_id"] and item["rate"] is not None
                else None
            )
            currency = item["report_currency"] or ""
            item["turnover_usd"] = (
                to_usd(item["turnover"], currency, rates) if item["report_id"] else None
            )
            item["share_usd"] = to_usd(item["expected_share"], currency, rates)
            result.append(item)
        return result

    def report(self, report_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT r.*, p.name AS partner_name, p.rate
                FROM reports r JOIN partners p ON p.id = r.partner_id
                WHERE r.id = ?
                """,
                (report_id,),
            ).fetchone()
            if not row:
                return None
            clients = db.execute(
                "SELECT location_id FROM report_clients WHERE report_id = ? ORDER BY location_id",
                (report_id,),
            ).fetchall()
            claims = db.execute(
                """
                SELECT location_id, kind, reason, detail,
                verification_status, verification_evidence
                FROM report_claims WHERE report_id = ?
                ORDER BY location_id, kind
                """,
                (report_id,),
            ).fetchall()
        item = dict(row)
        item["reasons"] = json.loads(item["reasons"])
        item["display_status"] = report_display_status(item)
        item["missing_active"] = json.loads(item["missing_active"])
        item["unknown_locations"] = json.loads(item["unknown_locations"])
        item["application_breakdown"] = json.loads(
            item.get("application_breakdown") or "[]"
        )
        item["clients"] = [client["location_id"] for client in clients]
        all_claims = [dict(claim) for claim in claims]
        idle_claims = [
            claim for claim in all_claims if claim["kind"] == "connected_not_active"
        ]
        item["claims"] = [
            claim for claim in all_claims if claim["kind"] != "connected_not_active"
        ]
        item["idle_claims"] = {
            "total": len(idle_claims),
            "contradicted": [
                claim["location_id"]
                for claim in idle_claims
                if claim["verification_status"] == "contradicted"
            ],
        }
        effective_rate = (
            item["contract_rate"]
            if item["contract_rate"] is not None
            else item["rate"]
        )
        item["expected_share"] = (
            round(item["turnover"] * effective_rate, 2)
            if effective_rate is not None
            else None
        )
        rates = self.fx_rates_for(item["period"])
        item["turnover_usd"] = to_usd(item["turnover"], item["currency"], rates)
        item["share_usd"] = to_usd(item["expected_share"], item["currency"], rates)
        item["reported_share_usd"] = to_usd(
            item["reported_share"], item["currency"], rates
        )
        with self.connect() as db:
            versions = db.execute(
                """
                SELECT version, turnover, currency, reported_share, contract_rate, client_count,
                       status, risk_level, risk_score, reasons, comment,
                       missing_active, unknown_locations, claims,
                       attachment, submitted_at, application_breakdown, line_items
                FROM report_versions
                WHERE report_id = ?
                ORDER BY version DESC
                """,
                (report_id,),
            ).fetchall()
        item["versions"] = [
            {
                **dict(version),
                "reasons": json.loads(version["reasons"]),
                "missing_active": json.loads(version["missing_active"] or "[]"),
                "unknown_locations": json.loads(version["unknown_locations"] or "[]"),
                "claims": json.loads(version["claims"] or "[]"),
                "application_breakdown": json.loads(
                    version["application_breakdown"] or "[]"
                ),
                "line_items": json.loads(version["line_items"] or "[]"),
            }
            for version in versions
        ]
        with self.connect() as db:
            events = db.execute(
                """
                SELECT workflow_status, note, actor, created_at
                FROM report_events
                WHERE report_id = ?
                ORDER BY id DESC
                """,
                (report_id,),
            ).fetchall()
        item["status_log"] = [dict(event) for event in events]
        return item

    def approve_report(self, report_id: int, approved_by: str = "admin") -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            report = db.execute(
                "SELECT id, workflow_status FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            if not report:
                return False
            db.execute(
                """
                UPDATE reports
                SET workflow_status = 'approved', approved_at = ?, approved_by = ?
                WHERE id = ?
                """,
                (now, approved_by, report_id),
            )
            if report["workflow_status"] != "approved":
                self._log_report_event(
                    db,
                    report_id,
                    "approved",
                    "approved",
                    actor=approved_by,
                    created_at=now,
                )
        return True

    def mark_posted(self, report_id: int, detail: str = "") -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            report = db.execute(
                """
                SELECT r.id, r.period, r.turnover, r.currency, r.reported_share,
                       r.contract_rate, r.workflow_status, p.name, p.rate
                FROM reports r JOIN partners p ON p.id = r.partner_id
                WHERE r.id = ?
                """,
                (report_id,),
            ).fetchone()
            if not report or report["workflow_status"] != "approved":
                return False
            rate = (
                report["contract_rate"]
                if report["contract_rate"] is not None
                else report["rate"]
            )
            expected = (
                round(report["turnover"] * rate, 2) if rate is not None else None
            )
            updated = db.execute(
                """
                UPDATE reports
                SET workflow_status = 'posted', posted_at = ?
                WHERE id = ? AND workflow_status = 'approved'
                """,
                (now, report_id),
            )
            if updated.rowcount != 1:
                return False
            db.execute(
                """
                INSERT INTO sheet_writes
                (report_id, period, partner_name, turnover, currency,
                 expected_share, status, detail, posted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    report["period"],
                    report["name"],
                    report["turnover"],
                    report["currency"],
                    expected,
                    "queued",
                    detail,
                    now,
                ),
            )
            self._log_report_event(
                db,
                report_id,
                "posted",
                "queued",
                actor="system",
                created_at=now,
            )
        return True

    def mark_posted_many(self, report_ids: list[int], detail: str = "") -> int:
        queued = 0
        for report_id in report_ids:
            if self.mark_posted(report_id, detail):
                queued += 1
        return queued

    def unapprove_report(self, report_id: int, actor: str = "admin") -> bool:
        """Reverse an approval so the admin can correct a mistake.

        Goes back to ``needs_review`` (the pre-approval workflow state) and
        clears the approval metadata. ``posted`` reports cannot be reverted
        via this method — admin must call ``unpost_report`` first.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            report = db.execute(
                "SELECT id, workflow_status FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            if not report or report["workflow_status"] != "approved":
                return False
            db.execute(
                """
                UPDATE reports
                SET workflow_status = 'needs_review',
                    approved_at = NULL, approved_by = NULL
                WHERE id = ?
                """,
                (report_id,),
            )
            self._log_report_event(
                db,
                report_id,
                "needs_review",
                "approval_reverted",
                actor=actor,
                created_at=now,
            )
        return True

    def unpost_report(self, report_id: int, actor: str = "admin") -> bool:
        """Reverse a 'posted' status back to 'approved' so admin can re-send.

        Sheet-write records are kept as-is (they document what was queued
        for Google Sheets); only the workflow status moves backwards.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            report = db.execute(
                "SELECT id, workflow_status FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            if not report or report["workflow_status"] != "posted":
                return False
            db.execute(
                """
                UPDATE reports
                SET workflow_status = 'approved', posted_at = NULL
                WHERE id = ?
                """,
                (report_id,),
            )
            self._log_report_event(
                db,
                report_id,
                "approved",
                "post_reverted",
                actor=actor,
                created_at=now,
            )
        return True

    def accrual_queue(self, period: str) -> list[dict]:
        items = []
        for item in self.dashboard(period):
            if item["workflow_status"] != "approved":
                continue
            items.append(
                {
                    **item,
                    "sheet_row": None,
                    "will_be": item["turnover"],
                    "share_will_be": item["expected_share"],
                }
            )
        return items

    def sheet_writes(self, period: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM sheet_writes WHERE period = ?
                ORDER BY posted_at DESC
                """,
                (period,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sheet_writes_by_status(self, status: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM sheet_writes
                WHERE status = ?
                ORDER BY posted_at ASC, id ASC
                """,
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def requeue_failed_sheet_writes(self, period: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE sheet_writes
                SET status = 'queued', detail = 'Retry requested'
                WHERE period = ? AND status = 'failed'
                """,
                (period,),
            )
        return cursor.rowcount

    def update_sheet_write(
        self,
        write_id: int,
        *,
        status: str,
        detail: str,
        sheet_cell: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE sheet_writes
                SET status = ?, detail = ?, sheet_cell = ?
                WHERE id = ?
                """,
                (status, detail, sheet_cell, write_id),
            )

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def digest_chat(self) -> str:
        return self.setting("digest_chat_id") or self.setting("kate_telegram_chat_id")

    def set_digest_chat(self, value: str) -> None:
        text = (value or "").strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :]
                break
        text = text.lstrip("@")
        if text and not text.lstrip("-").isdigit():
            text = f"@{text}"
        self.set_setting("digest_chat_id", text)

    def bind_telegram_chat(self, token: str, chat_id: str) -> dict | None:
        partner = self.partner_by_token(token)
        if not partner:
            return None
        with self.connect() as db:
            db.execute(
                "UPDATE partners SET telegram_chat_id = ? WHERE id = ?",
                (str(chat_id), partner["id"]),
            )
        return self.partner(partner["id"])

    def bind_telegram_chat_by_username(
        self, username: str, chat_id: str
    ) -> dict | None:
        handle = username.strip().lstrip("@")
        if not handle:
            return None
        with self.connect() as db:
            # A single Telegram account can own multiple partner accounts
            # (e.g. one person running several integration projects), so we
            # link the chat_id to every partner that uses this username.
            rows = db.execute(
                """
                SELECT id FROM partners
                WHERE lower(replace(telegram, '@', '')) = lower(?)
                ORDER BY id
                """,
                (handle,),
            ).fetchall()
            if not rows:
                return None
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            db.execute(
                f"UPDATE partners SET telegram_chat_id = ? WHERE id IN ({placeholders})",
                (str(chat_id), *ids),
            )
            refreshed = db.execute(
                "SELECT * FROM partners WHERE id = ?", (ids[0],)
            ).fetchone()
        return dict(refreshed) if refreshed else None

    def unlink_telegram_chat(self, partner_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE partners SET telegram_chat_id = '' WHERE id = ? AND active = 1",
                (partner_id,),
            )
            return cursor.rowcount > 0

    def propagate_telegram_chat_by_handle(self, partner_id: int) -> dict | None:
        # When an admin links a new partner to a Telegram handle that is
        # already bound to a chat on another partner row, copy that chat
        # id over so the new partner can be messaged without forcing the
        # user to press /start again.
        with self.connect() as db:
            partner = db.execute(
                "SELECT id, telegram, telegram_chat_id FROM partners WHERE id = ?",
                (partner_id,),
            ).fetchone()
            if not partner:
                return None
            handle = (partner["telegram"] or "").strip().lstrip("@")
            if not handle or partner["telegram_chat_id"]:
                return None
            existing = db.execute(
                """
                SELECT telegram_chat_id FROM partners
                WHERE id != ?
                  AND telegram_chat_id IS NOT NULL
                  AND telegram_chat_id <> ''
                  AND lower(replace(telegram, '@', '')) = lower(?)
                ORDER BY id LIMIT 1
                """,
                (partner_id, handle),
            ).fetchone()
            if not existing:
                return None
            db.execute(
                "UPDATE partners SET telegram_chat_id = ? WHERE id = ?",
                (existing["telegram_chat_id"], partner_id),
            )
            refreshed = db.execute(
                "SELECT * FROM partners WHERE id = ?", (partner_id,)
            ).fetchone()
        return dict(refreshed) if refreshed else None

    def notice_already_sent(self, partner_id: int, period: str, notice_key: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT id FROM notice_sends
                WHERE partner_id = ? AND period = ? AND notice_key = ? AND status = 'sent'
                """,
                (partner_id, period, notice_key),
            ).fetchone()
        return row is not None

    def claim_notice(
        self, partner_id: int, period: str, notice_key: str, *, force: bool = False
    ) -> bool:
        """Atomically reserve a notice so parallel workers cannot send it twice."""
        if force:
            return True
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT id, status, sent_at FROM notice_sends
                WHERE partner_id = ? AND period = ? AND notice_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (partner_id, period, notice_key),
            ).fetchone()
            if row and row["status"] == "sent":
                return False
            if row and row["status"] == "sending":
                try:
                    claimed_at = datetime.fromisoformat(row["sent_at"])
                except (TypeError, ValueError):
                    claimed_at = datetime.now(UTC)
                if datetime.now(UTC) - claimed_at < NOTICE_CLAIM_TIMEOUT:
                    return False
            if row:
                db.execute(
                    "UPDATE notice_sends SET status = 'sending', detail = '', sent_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            else:
                db.execute(
                    """
                    INSERT INTO notice_sends
                    (partner_id, period, notice_key, status, detail, sent_at)
                    VALUES (?, ?, ?, 'sending', '', ?)
                    """,
                    (partner_id, period, notice_key, now),
                )
        return True

    def log_notice_send(
        self,
        partner_id: int,
        period: str,
        notice_key: str,
        status: str,
        detail: str,
    ) -> None:
        with self.connect() as db:
            latest = db.execute(
                """
                SELECT id FROM notice_sends
                WHERE partner_id = ? AND period = ? AND notice_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (partner_id, period, notice_key),
            ).fetchone()
            if latest:
                db.execute(
                    """
                    UPDATE notice_sends
                    SET status = ?, detail = ?, sent_at = ?
                    WHERE id = ?
                    """,
                    (status, detail, datetime.now(UTC).isoformat(), latest["id"]),
                )
                return
            db.execute(
                """
                INSERT INTO notice_sends
                (partner_id, period, notice_key, status, detail, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    partner_id,
                    period,
                    notice_key,
                    status,
                    detail,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def replace_reconciliation_base(
        self,
        period: str,
        uploaded_at: str,
        uploaded_by: str,
        file_path: str,
        rows: list[BaseRow],
        notes: str = "",
    ) -> None:
        """Replace the reconciliation base for ``period`` atomically.

        Deletes any existing rows for the period then inserts the new ones,
        and upserts the ``reconciliation_bases`` metadata row.
        """
        with self.connect() as db:
            db.execute(
                "DELETE FROM reconciliation_base_rows WHERE period = ?",
                (period,),
            )
            if rows:
                db.executemany(
                    """
                    INSERT INTO reconciliation_base_rows
                    (period, application_id, location_id, turnover, currency,
                     active, source_row)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            period,
                            row.application_id,
                            row.location_id,
                            row.turnover,
                            row.currency,
                            int(bool(row.active)),
                            row.source_row,
                        )
                        for row in rows
                    ],
                )
            db.execute(
                """
                INSERT INTO reconciliation_bases
                (period, uploaded_at, uploaded_by, file_path, row_count, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(period) DO UPDATE SET
                    uploaded_at = excluded.uploaded_at,
                    uploaded_by = excluded.uploaded_by,
                    file_path = excluded.file_path,
                    row_count = excluded.row_count,
                    notes = excluded.notes
                """,
                (period, uploaded_at, uploaded_by, file_path, len(rows), notes),
            )

    def get_reconciliation_base(self, period: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM reconciliation_bases WHERE period = ?",
                (period,),
            ).fetchone()
        if row is None:
            return None
        return {
            "period": row["period"],
            "uploaded_at": row["uploaded_at"],
            "uploaded_by": row["uploaded_by"],
            "file_path": row["file_path"],
            "row_count": row["row_count"],
            "notes": row["notes"],
        }

    def get_reconciliation_base_rows(self, period: str) -> list[dict]:
        """Return all rows of the reconciliation base for ``period``.

        Each row is a dict with keys ``application_id``, ``location_id``,
        ``turnover``, ``currency``, ``active``. The list is empty if no base
        exists or the base has no rows.
        """
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT application_id, location_id, turnover, currency, active
                FROM reconciliation_base_rows
                WHERE period = ?
                ORDER BY location_id
                """,
                (period,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_report_clients(self, report_id: int) -> list[str]:
        """Return the list of ``location_id`` strings for a report."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_id FROM report_clients
                WHERE report_id = ?
                ORDER BY location_id
                """,
                (report_id,),
            ).fetchall()
        return [str(row["location_id"]) for row in rows]

    def list_reports_for_period(self, period: str) -> list[int]:
        """Return ``report.id`` values for all reports of ``period``.

        Ordered ascending by ``id`` so the revalidation worker iterates in a
        stable, chronological order.
        """
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id FROM reports WHERE period = ?
                ORDER BY id ASC
                """,
                (period,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def update_report_assessment(
        self,
        report_id: int,
        level: str,
        score: int,
        reasons_json: str,
        missing_active_json: str,
        unknown_locations_json: str,
    ) -> None:
        """Update the assess result on an existing report row.

        Used by the revalidation worker after recomputing ``assess`` for a
        report with the freshly loaded reconciliation base. Does not touch
        any other column (turnover, claims, workflow status, etc.).
        """
        with self.connect() as db:
            db.execute(
                """
                UPDATE reports
                SET risk_level = ?, risk_score = ?,
                    reasons = ?, missing_active = ?, unknown_locations = ?
                WHERE id = ?
                """,
                (
                    level,
                    int(score),
                    reasons_json,
                    missing_active_json,
                    unknown_locations_json,
                    report_id,
                ),
            )

    def get_revalidation_task(self, task_id: str) -> dict | None:
        """Return the ``revalidation_tasks`` row for ``task_id`` or ``None``."""
        with self.connect() as db:
            row = db.execute(
                """
                SELECT id, period, total, processed, current_report, status,
                       started_at, finished_at, summary
                FROM revalidation_tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["summary"] = json.loads(item["summary"] or "{}")
        except json.JSONDecodeError:
            item["summary"] = {}
        return item

    def list_active_revalidation_tasks(self, period: str) -> list[dict]:
        """Return pending/running revalidation tasks for ``period``."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, period, total, processed, current_report, status,
                       started_at, finished_at, summary
                FROM revalidation_tasks
                WHERE period = ? AND status IN ('pending', 'running')
                ORDER BY id
                """,
                (period,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_revalidation_task(self, task_id: str, period: str) -> None:
        """Insert a new ``pending`` revalidation task row."""
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO revalidation_tasks
                (id, period, total, processed, current_report, status,
                 started_at, finished_at, summary)
                VALUES (?, ?, 0, 0, '', 'pending', '', '', '{}')
                """,
                (task_id, period),
            )

    def cancel_revalidation_tasks(self, period: str) -> int:
        """Mark all pending/running tasks for ``period`` as ``cancelled``.

        Returns the number of rows updated. Used when a fresh base upload
        arrives while a previous revalidation is still in flight, so the
        stale worker stops touching the rows table.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE revalidation_tasks
                SET status = 'cancelled', finished_at = ?
                WHERE period = ? AND status IN ('pending', 'running')
                """,
                (now, period),
            )
        return int(cursor.rowcount or 0)

    def update_revalidation_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        total: int | None = None,
        processed: int | None = None,
        current_report: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        summary: str | None = None,
    ) -> None:
        """Update mutable fields of a revalidation task. No-op if task is gone."""
        assignments: list[str] = []
        values: list[object] = []
        for column, value in (
            ("status", status),
            ("total", total),
            ("processed", processed),
            ("current_report", current_report),
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("summary", summary),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if not assignments:
            return
        values.append(task_id)
        with self.connect() as db:
            db.execute(
                f"UPDATE revalidation_tasks SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def get_report_for_revalidation(self, report_id: int) -> dict | None:
        """Return the columns the revalidation worker needs to re-run ``assess``.

        Includes the partner's contract rate (from the partner record if the
        report does not store one) and the previous month's turnover /
        client_count, mirroring the pattern in ``_reassess_report``.
        """
        with self.connect() as db:
            row = db.execute(
                """
                SELECT r.id, r.partner_id, r.period, r.turnover,
                       r.reported_share,
                       COALESCE(r.contract_rate, p.rate) AS contract_rate,
                       r.risk_level, r.report_source,
                       p.name AS partner_name
                FROM reports r JOIN partners p ON p.id = r.partner_id
                WHERE r.id = ?
                """,
                (report_id,),
            ).fetchone()
            if not row:
                return None
            previous = db.execute(
                """
                SELECT turnover, client_count FROM reports
                WHERE partner_id = ? AND period < ?
                  AND id != ?
                ORDER BY period DESC LIMIT 1
                """,
                (row["partner_id"], row["period"], report_id),
            ).fetchone()
        item = dict(row)
        if previous is not None:
            item["previous_turnover"] = previous["turnover"]
            item["previous_client_count"] = previous["client_count"]
        else:
            item["previous_turnover"] = None
            item["previous_client_count"] = None
        return item

from __future__ import annotations

import logging
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import urlparse

from revshare.fx import valid_period
from revshare.mcp_client import MCPClient, MCPError, access_token
from revshare.mysql import mysql_connection, mysql_configured
from revshare.store import Store

APP_ID_RE = re.compile(r"/app/(\d+)/?$")
MARKETPLACE_APP_ID_RE = re.compile(r"/(?:e|m)/mp_(\d+)(?:_[^/]+)?/?$")

log = logging.getLogger("revshare.altegio_sync")


@dataclass
class PartnerSyncResult:
    partner_id: int
    partner_name: str
    application_ids: list[int]
    connected: int
    active: int
    payment: int


@dataclass
class SyncResult:
    period: str
    partners_total: int = 0
    partners_synced: int = 0
    partners_skipped: list[dict[str, str]] = field(default_factory=list)
    partners: list[PartnerSyncResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_application_id(url: str) -> int | None:
    path = urlparse(url.strip()).path
    for pattern in (APP_ID_RE, MARKETPLACE_APP_ID_RE):
        match = pattern.search(path)
        if match:
            return int(match.group(1))
    return None


def application_ids_for_partner(store: Store, partner_id: int) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for link in store.application_links(partner_id):
        app_id = parse_application_id(link["url"])
        if app_id is None or app_id in seen:
            continue
        seen.add(app_id)
        ids.append(app_id)
    return ids


def period_bounds(period: str) -> tuple[str, str]:
    checked = valid_period(period)
    if not checked:
        raise ValueError("invalid_period")
    year, month = (int(part) for part in checked.split("-"))
    start = date(year, month, 1).isoformat()
    if month == 12:
        end_exclusive = date(year + 1, 1, 1).isoformat()
    else:
        end_exclusive = date(year, month + 1, 1).isoformat()
    return start, end_exclusive


def _format_dt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    return str(value)


def _fetch_usage_rows(
    cursor: Any, application_ids: list[int], period: str
) -> dict[str, dict[str, object]]:
    start, end_exclusive = period_bounds(period)
    placeholders = ",".join(["%s"] * len(application_ids))
    params = tuple(application_ids)

    cursor.execute(
        f"""
        SELECT l.salon_id, l.created_at AS connected_at, s.title AS salon_name
        FROM marketplace_application_salon_links l
        JOIN salons s ON s.id = l.salon_id
        WHERE l.marketplace_application_id IN ({placeholders})
          AND l.status IN ('active', 'freezed')
          AND l.created_at < %s
          AND s.deleted = 0
          AND s.trash = 0
        """,
        (*params, end_exclusive),
    )
    connected_rows = cursor.fetchall()

    cursor.execute(
        f"""
        SELECT l.salon_id, MAX(l.changed_at) AS last_activity_at
        FROM marketplace_application_log l
        JOIN salons s ON s.id = l.salon_id
        WHERE l.marketplace_application_id IN ({placeholders})
          AND l.changed_at >= %s
          AND l.changed_at < %s
          AND s.deleted = 0
          AND s.trash = 0
        GROUP BY l.salon_id
        """,
        (*params, start, end_exclusive),
    )
    active_rows = {str(row["salon_id"]): row for row in cursor.fetchall()}

    cursor.execute(
        f"""
        SELECT salon_id, MAX(payment_date) AS last_payment_at
        FROM marketplace_payment_transactions
        WHERE marketplace_application_id IN ({placeholders})
          AND payment_date >= %s
          AND payment_date < %s
        GROUP BY salon_id
        """,
        (*params, start, end_exclusive),
    )
    transaction_rows = {str(row["salon_id"]): row for row in cursor.fetchall()}

    cursor.execute(
        f"""
        SELECT salon_id, MAX(payment_date) AS last_payment_at
        FROM marketplace_payments
        WHERE marketplace_application_id IN ({placeholders})
          AND processing_status = 'completed'
          AND payment_date >= %s
          AND payment_date < %s
        GROUP BY salon_id
        """,
        (*params, start, end_exclusive),
    )
    payment_rows = {str(row["salon_id"]): row for row in cursor.fetchall()}

    locations: dict[str, dict[str, object]] = {}
    for row in connected_rows:
        location_id = str(row["salon_id"])
        locations[location_id] = {
            "name": row.get("salon_name") or "",
            "connected_at": _format_dt(row.get("connected_at")),
            "last_api_at": "",
            "has_api_amounts": 0,
        }

    for location_id, row in active_rows.items():
        entry = locations.setdefault(
            location_id,
            {"name": "", "connected_at": "", "last_api_at": "", "has_api_amounts": 0},
        )
        entry["last_api_at"] = _format_dt(row.get("last_activity_at"))

    for location_id, row in {**transaction_rows, **payment_rows}.items():
        entry = locations.setdefault(
            location_id,
            {"name": "", "connected_at": "", "last_api_at": "", "has_api_amounts": 0},
        )
        payment_at = _format_dt(row.get("last_payment_at"))
        if payment_at and (
            not entry["last_api_at"] or payment_at > str(entry["last_api_at"])
        ):
            entry["last_api_at"] = payment_at
        entry["has_api_amounts"] = 1

    return locations


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


class _MCPQueryCursor:
    """Small DB-API-shaped adapter for the existing Altegio query code."""

    def __init__(self, client: MCPClient):
        self.client = client
        self.rows: list[dict[str, object]] = []

    def execute(self, statement: str, params: tuple[object, ...] = ()) -> None:
        parts = statement.split("%s")
        if len(parts) != len(params) + 1:
            raise MCPError("MCP query parameters do not match the SQL")
        sql = parts[0]
        for value, tail in zip(params, parts[1:]):
            sql += _sql_literal(value) + tail
        self.rows = []
        offset = 0
        while True:
            page_sql = f"{sql.rstrip().rstrip(';')} LIMIT 1000 OFFSET {offset}"
            page = self.client.query_rows(page_sql, database="salon", limit=1000)
            self.rows.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
            if offset > 100_000:
                raise MCPError("MCP query returned more than 100000 rows")

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


def _fetch_usage_rows_mcp(
    client: MCPClient, application_ids: list[int], period: str
) -> dict[str, dict[str, object]]:
    return _fetch_usage_rows(_MCPQueryCursor(client), application_ids, period)


def _sync_with_fetcher(
    store: Store,
    checked: str,
    fetch_usage: Callable[[list[int], str], dict[str, dict[str, object]]],
) -> SyncResult:
    result = SyncResult(period=checked)
    partners = list(store.partners())
    result.partners_total = len(partners)

    for partner in partners:
        app_ids = application_ids_for_partner(store, partner["id"])
        if not app_ids:
            result.partners_skipped.append(
                {"partner": partner["name"], "reason": "no_application_url"}
            )
            continue
        locations: dict[str, dict[str, object]] = {}
        try:
            for application_id in app_ids:
                application_locations = fetch_usage([application_id], checked)
                connected_for_app = sorted(
                    location_id
                    for location_id, facts in application_locations.items()
                    if facts.get("connected_at")
                )
                active_for_app = sorted(
                    active_rows_ids(application_locations, active_only=True)
                )
                payment_for_app = sorted(
                    location_id
                    for location_id, facts in application_locations.items()
                    if facts.get("has_api_amounts")
                )
                store.replace_application_usage(
                    partner["id"],
                    checked,
                    application_id,
                    connected_for_app,
                    active_for_app,
                    payment_for_app,
                    location_details=application_locations,
                )
                for location_id, facts in application_locations.items():
                    current = locations.setdefault(
                        location_id,
                        {
                            "name": "",
                            "connected_at": "",
                            "last_api_at": "",
                            "has_api_amounts": 0,
                        },
                    )
                    for key in ("name", "connected_at", "last_api_at"):
                        if facts.get(key) and not current.get(key):
                            current[key] = facts[key]
                    current["has_api_amounts"] = int(
                        bool(current.get("has_api_amounts"))
                        or bool(facts.get("has_api_amounts"))
                    )
        except Exception as error:
            result.errors.append(f"{partner['name']}: {error}")
            continue

        connected_ids = sorted(
            location_id
            for location_id, facts in locations.items()
            if facts.get("connected_at")
        )
        active_ids = sorted(active_rows_ids(locations, active_only=True))
        payment_ids = sorted(
            location_id
            for location_id, facts in locations.items()
            if facts.get("has_api_amounts")
        )

        store.replace_usage(
            partner["id"],
            checked,
            connected_ids,
            active_ids,
            payment_ids,
            source="altegio_bi",
            location_details=locations,
        )
        result.partners_synced += 1
        result.partners.append(
            PartnerSyncResult(
                partner_id=partner["id"],
                partner_name=partner["name"],
                application_ids=app_ids,
                connected=len(connected_ids),
                active=len(active_ids),
                payment=len(payment_ids),
            )
        )

    return result


def sync_period(store: Store, period: str) -> SyncResult:
    """Deliberate no-op post-pivot to manual reconciliation base uploads.

    After the pivot away from the Altegio BI snapshot, partner reports are
    reconciled against a monthly base file that the admin uploads through
    the new admin page (``/admin/reconciliation``). The hourly worker and
    the ``sync-altegio`` CLI command therefore no longer need to pull
    anything from MySQL or the MCP BI server — this function used to be
    that pull.

    It is kept as a no-op (not removed) so the historical sync path is
    recoverable if anyone ever wants to revert to the BI snapshot: see
    ``docs/epic-reconciliation.md`` for the rationale. The full MySQL and
    MCP write helpers above are intentionally left in place so the
    escape hatch stays buildable.
    """
    del store  # intentionally unused now
    checked = valid_period(period)
    if not checked:
        raise ValueError("invalid_period")
    log.info(
        "Altegio usage sync is a no-op: reconciliation runs against the "
        "admin-uploaded monthly base instead"
    )
    return SyncResult(
        period=checked,
        partners_synced=0,
        partners_skipped=[],
        errors=[],
    )


def active_rows_ids(
    locations: dict[str, dict[str, object]], *, active_only: bool
) -> list[str]:
    if not active_only:
        return sorted(locations)
    return sorted(
        location_id
        for location_id, facts in locations.items()
        if facts.get("last_api_at")
    )

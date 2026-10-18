from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hmac
import io
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from revshare.countries import (
    ALTEGIO_LEGAL_ENTITIES,
    APP_CATEGORIES,
    canonical_country,
    countries_for,
    country_label,
)
from revshare.altegio import (
    apply_snapshot,
    fetch_snapshot,
    load_snapshot_file,
    local_snapshot_path,
    snapshot_url,
)
from revshare.altegio_sync import parse_application_id, sync_period
from revshare.application_breakdown import add_usage_checks, breakdown_for_report
from revshare.mysql import mysql_configured
from revshare.dispatch import send_due_notices, send_partner_notice
from revshare.mcp_client import (
    MCPError,
    authorization_url,
    exchange_code,
    register_client,
    pkce_pair,
    store_token_payload,
)
from revshare.extract import (
    BaseRow,
    _normalise_report_reason,
    parse_partner_report,
    parse_reconciliation_base,
)
from revshare.fx import SUPPORTED_CURRENCIES, fetch_month_average_rates, valid_period
from revshare.i18n import (
    ADMIN_LANGUAGES,
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    months_word,
    translator,
)
from revshare.notifications import (
    NOTICE_KEYS,
    NOTICE_LANGUAGES,
    PARTNER_NOTICE_KEYS,
    recent_periods,
    reporting_period,
    schedule_for_period,
    settings_from_form,
)
from revshare.report_template import build_report_template_xlsx
from revshare.secrets import load_secrets, telegram_token
from revshare.timezone import format_local_datetime, seconds_until_notice_send, utc_now_iso
from revshare.sheet_sync import flush_queued_sheet_writes
from revshare import sheets
from revshare.revalidation import schedule_revalidation
from revshare.store import (
    CLAIM_REASON_KEYS,
    DEFAULT_DB,
    Store,
    assess,
    discrepancy_items,
    normalize_application_links,
    normalize_ids,
)


def _load_connected_active_ids_for_test(
    store: Store, period: str
) -> tuple[set[str], set[str], bool]:
    """Test-only mirror of the request-scoped helper in ``create_app``.

    Kept here (module scope) so unit tests can call it without going
    through a FastAPI app. The two implementations must stay in sync;
    the request-scoped one is the source of truth at runtime.

    After the pivot there is no ``usage_snapshots`` fallback any more:
    when no base has been uploaded for the period, the report parks as
    ``pending_base`` with empty id sets.
    """
    base_rows = store.get_reconciliation_base_rows(period)
    base_meta = store.get_reconciliation_base(period)
    if base_meta and base_rows:
        connected = {str(row["location_id"]) for row in base_rows}
        active = {
            str(row["location_id"]) for row in base_rows if int(row["active"])
        }
        return connected, active, True
    if base_meta:
        return set(), set(), True
    return set(), set(), False


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_REPORT_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SNAPSHOT_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_BASE_UPLOAD_BYTES = 10 * 1024 * 1024
log = logging.getLogger("revshare.web")


PARTNER_TABS = ("main", "history", "settings")
PARTNER_AUTOMATION_STATUSES = ("manual", "transition", "automatic")
REVIEW_TABS = ("summary", "differences")
CURRENCY_SYMBOLS = {
    "AED": "د.إ",
    "AMD": "֏",
    "CZK": "Kč",
    "EUR": "€",
    "GBP": "£",
    "KZT": "₸",
    "PLN": "zł",
    "RUB": "₽",
    "TRY": "₺",
    "UAH": "₴",
    "USD": "$",
}


def format_partner_amount(value: object, currency: object = "") -> str:
    if value is None or value == "":
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return f"{value} {currency or ''}".strip()
    decimals = 2 if amount % 1 else 0
    formatted = f"{amount:,.{decimals}f}".replace(",", " ")
    symbol = CURRENCY_SYMBOLS.get(str(currency or "").upper(), str(currency or ""))
    return f"{formatted} {symbol}".strip()


def review_reason_summary(
    required_discrepancies: list[dict], report_reasons: dict[str, str]
) -> tuple[int, int]:
    total = len(required_discrepancies)
    filled = sum(
        1
        for item in required_discrepancies
        if report_reasons.get(str(item["location_id"]))
    )
    return filled, total


PAYMENT_STATUS_LABELS = {
    "paid": "Оплата получена",
    "trial": "Триал",
    "free_trial": "Бесплатный месяц",
    "pay_next_month": "Оплата в следующем месяце",
    "disconnected_before_month": "Аккаунт закрыт или приложение отключено",
    "connected_after_month": "Подключён после отчётного месяца",
    "no_payment": "Не платит за приложение",
    "file_error": "Ошибка в отчёте",
    "other": "Другое",
}


def partner_registry_issues(
    report_ids: set[str],
    reference_ids: set[str],
    payment_ids: set[str],
    statuses: dict[str, str],
) -> list[dict[str, object]]:
    """Return file completeness and payment-status issues for a partner register."""
    issues: list[dict[str, object]] = []
    for location_id in sorted(reference_ids - report_ids):
        issues.append(
            {
                "location_id": location_id,
                "kind": "active_not_reported",
                "required": True,
            }
        )
    for location_id in sorted((report_ids & reference_ids) - payment_ids):
        status = statuses.get(location_id, "")
        if not status:
            issues.append(
                {
                    "location_id": location_id,
                    "kind": "payment_status_missing",
                    "required": True,
                }
            )
        elif status == "paid":
            issues.append(
                {
                    "location_id": location_id,
                    "kind": "payment_not_found",
                    "required": True,
                }
            )
    for location_id in sorted(report_ids - reference_ids):
        issues.append(
            {
                "location_id": location_id,
                "kind": "reported_not_connected",
                "required": False,
            }
        )
    return issues


def application_registry_check(
    items: list[dict[str, object]],
    store: Store,
    partner_id: int,
    period: str,
    statuses: dict[str, str],
) -> dict[str, object] | None:
    """Compare application/location pairs when an application snapshot exists."""
    expected: set[tuple[int, str]] = set()
    reported: set[tuple[int, str]] = set()
    paid: set[tuple[int, str]] = set()
    names: dict[int, str] = {}
    comparable = False
    for item in items:
        application_id = item.get("application_id")
        if not isinstance(application_id, int):
            continue
        usage = store.application_usage(partner_id, period, application_id)
        if usage is None:
            continue
        comparable = True
        _connected, active = usage
        expected.update((application_id, location_id) for location_id in active)
        reported.update(
            (application_id, str(location_id))
            for location_id in item.get("location_ids") or []
        )
        paid.update(
            (application_id, location_id)
            for location_id in store.application_payment_ids(partner_id, period, application_id)
        )
        names[application_id] = str(
            item.get("application_name") or item.get("application_ref") or application_id
        )
    if not comparable:
        return None

    issues: list[dict[str, object]] = []
    for application_id, location_id in sorted(expected - reported):
        issues.append(
            {
                "application_id": application_id,
                "application": names.get(application_id, str(application_id)),
                "location_id": location_id,
                "kind": "connected_not_reported",
                "required": True,
            }
        )
    for application_id, location_id in sorted(expected & reported):
        if (application_id, location_id) in paid:
            continue
        status = statuses.get(location_id, "")
        if not status:
            kind = "payment_status_missing"
        elif status == "paid":
            kind = "payment_not_found"
        else:
            continue
        issues.append(
            {
                "application_id": application_id,
                "application": names.get(application_id, str(application_id)),
                "location_id": location_id,
                "kind": kind,
                "required": True,
            }
        )
    for application_id, location_id in sorted(reported - expected):
        issues.append(
            {
                "application_id": application_id,
                "application": names.get(application_id, str(application_id)),
                "location_id": location_id,
                "kind": "reported_not_connected",
                "required": False,
            }
        )
    return {
        "issues": issues,
        "report_count": len(reported),
        "registry_count": len(expected),
        "payment_ids": {location_id for _application_id, location_id in paid},
    }


def partner_payment_summary(
    report_ids: set[str],
    active_ids: set[str],
    reference_ids: set[str],
    payment_ids: set[str],
    statuses: dict[str, str],
) -> list[dict[str, object]]:
    no_payment = (report_ids & reference_ids) - payment_ids
    return [
        {"key": "active_paid", "count": len(report_ids & active_ids & payment_ids)},
        {"key": "active_no_payment", "count": len(report_ids & active_ids - payment_ids)},
        {
            "key": "inactive_paid",
            "count": len(report_ids & (reference_ids - active_ids) & payment_ids),
        },
        {
            "key": "inactive_no_payment",
            "count": len(report_ids & (reference_ids - active_ids) - payment_ids),
        },
    ] + [
        {"key": f"status_{status}", "count": sum(1 for location_id in no_payment if statuses.get(location_id) == status)}
        for status in sorted(set(statuses.values()) - {"paid"})
        if any(statuses.get(location_id) == status for location_id in no_payment)
    ]


def partner_report_rows(
    report_ids: list[str],
    included_ids: set[str],
    connected_ids: set[str],
    active_ids: set[str],
    payment_ids: set[str],
    statuses: dict[str, str],
    status_details: dict[str, str],
    location_details: dict[str, dict[str, object]],
    application_names: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    rows = []
    for location_id in report_ids:
        status = statuses.get(location_id, "")
        if not status and location_id in payment_ids:
            status = "paid"
        rows.append(
            {
                "location_id": location_id,
                "application": (application_names or {}).get(location_id, ""),
                "name": location_details.get(location_id, {}).get("location_name") or "",
                "included": location_id in included_ids,
                "connected": location_id in connected_ids,
                "active": location_id in active_ids,
                "payment": location_id in payment_ids,
                "status": status,
                "detail": status_details.get(location_id, ""),
            }
        )
    return rows


def application_names_by_location(items: list[dict[str, object]]) -> dict[str, str]:
    names: dict[str, list[str]] = {}
    for item in items:
        name = str(item.get("application_name") or item.get("application_ref") or "").strip()
        if not name:
            continue
        for location_id in item.get("location_ids") or []:
            values = names.setdefault(str(location_id), [])
            if name not in values:
                values.append(name)
    return {location_id: ", ".join(values) for location_id, values in names.items()}


def review_active_tab(
    request: Request,
    *,
    required_count: int,
    form_error: str | None = None,
) -> str:
    tab = request.query_params.get("tab", "")
    if tab in REVIEW_TABS:
        return tab
    if form_error == "explanations_required":
        return "differences"
    return "summary"


DASHBOARD_STATUSES = (
    "all", "missing", "submitted", "approved", "review", "verified",
    "checked", "discrepancies", "manual", "attention",
)
DASHBOARD_RISKS = ("all", "green", "yellow", "red")
STATUS_ORDER = {"requires_review": 0, "verified": 1}
DASHBOARD_SORTS = {
    "name": lambda item: (item["name"] or "").lower(),
    "status": lambda item: (
        STATUS_ORDER.get(item["status"], 2) if item["report_id"] else 3
    ),
    "turnover": lambda item: item.get("turnover_usd")
    if item["report_id"]
    else None,
    "share": lambda item: item.get("share_usd") if item["report_id"] else None,
    "clients": lambda item: item["client_count"] if item["report_id"] else None,
    "active": lambda item: item["active_count"],
    "risk": lambda item: item["risk_score"] if item["report_id"] else None,
    "status_updated": lambda item: (
        item.get("status_updated_at") if item["report_id"] else None
    ),
}
DEFAULT_SORT = "name"


def current_period() -> str:
    return reporting_period()


def history_chart(history: list[dict], bars: int = 12) -> list[dict]:
    """Oldest first bars for revshare in dollars and connected locations."""
    recent = list(reversed(history[:bars]))
    usd_values = [item["share_usd"] or 0 for item in recent]
    connected_values = [item["connected_count"] or 0 for item in recent]
    top_usd = max(usd_values) if usd_values else 0
    top_connected = max(connected_values) if connected_values else 0
    return [
        {
            "period": item["period"],
            "usd": usd,
            "connected": connected,
            "usd_height": round(usd / top_usd * 100) if top_usd else 0,
            "connected_height": round(connected / top_connected * 100)
            if top_connected
            else 0,
        }
        for item, usd, connected in zip(recent, usd_values, connected_values)
    ]


def long_idle_locations(
    store: Store, partner_id: int, period: str, discrepancies: list[dict]
) -> list[dict]:
    """Idle installations that crossed the consecutive-month threshold."""
    idle_ids = {
        item["location_id"]
        for item in discrepancies
        if item["kind"] == "connected_not_active"
    }
    return [
        candidate
        for candidate in store.disconnect_candidates(period, partner_id=partner_id)
        if candidate["location_id"] in idle_ids
    ]


def report_is_approved(item: dict) -> bool:
    return item.get("workflow_status") in ("approved", "posted")


def revshare_group(items: list[dict], *, in_usd: bool = False) -> dict:
    """Sum revshare for a set of reports, in one currency when they agree."""
    currencies = {item["report_currency"] for item in items if item["report_currency"]}
    usd = sum(item["share_usd"] or 0 for item in items)
    if in_usd:
        return {
            "count": len(items),
            "amount": usd,
            "currency": "USD",
            "converted": any(
                item.get("report_currency") and item["report_currency"] != "USD"
                for item in items
            ),
            "usd": usd,
        }
    if len(currencies) == 1:
        currency = currencies.pop()
        return {
            "count": len(items),
            "amount": sum(item["expected_share"] or 0 for item in items),
            "currency": currency,
            "converted": False,
            "usd": usd,
        }
    return {
        "count": len(items),
        "amount": usd,
        "currency": "USD",
        "converted": bool(currencies),
        "usd": usd,
    }


def dashboard_filters(
    status: str = "all",
    risk: str = "all",
    q: str = "",
) -> dict:
    return {
        "status": status if status in DASHBOARD_STATUSES else "all",
        "risk": risk if risk in DASHBOARD_RISKS else "all",
        "q": (q or "").strip(),
    }


def filters_are_active(filters: dict) -> bool:
    return (
        filters["status"] != "all"
        or filters["risk"] != "all"
        or bool(filters["q"])
    )


def sort_dashboard(items: list[dict], sort: str, direction: str) -> list[dict]:
    key = DASHBOARD_SORTS.get(sort, DASHBOARD_SORTS[DEFAULT_SORT])
    known = [item for item in items if key(item) is not None]
    unknown = [item for item in items if key(item) is None]
    ordered = sorted(known, key=key, reverse=direction == "desc")
    return ordered + sorted(unknown, key=DASHBOARD_SORTS[DEFAULT_SORT])


def sort_links(base: dict, sort: str, direction: str) -> dict:
    links = {}
    for column in DASHBOARD_SORTS:
        numeric = column not in {"name", "status"}
        first_direction = "desc" if numeric else "asc"
        active = column == sort
        if active:
            next_direction = "asc" if direction == "desc" else "desc"
        else:
            next_direction = first_direction
        links[column] = {
            "url": "/?" + urlencode({**base, "sort": column, "dir": next_direction}),
            "active": active,
            "direction": direction if active else "",
        }
    return links


def matches_dashboard_filters(item: dict, filters: dict) -> bool:
    status = filters["status"]
    submitted = item["report_id"] is not None
    if status in {"checked", "discrepancies", "manual", "attention"} and (
        not submitted or item.get("display_status") != status
    ):
        return False
    if status == "submitted" and not submitted:
        return False
    if status == "missing" and submitted:
        return False
    if status == "approved" and (
        not submitted
        or item.get("workflow_status") not in ("approved", "posted")
    ):
        return False
    if status == "review" and (
        not submitted
        or item.get("workflow_status") in ("approved", "posted")
        or item["status"] != "requires_review"
    ):
        return False
    if status == "verified" and (
        not submitted
        or item.get("workflow_status") in ("approved", "posted")
        or item["status"] != "verified"
    ):
        return False
    if filters["risk"] != "all" and (
        not submitted or item["risk_level"] != filters["risk"]
    ):
        return False
    search = filters["q"]
    if search:
        haystack = " ".join(
            str(item.get(field) or "")
            for field in ("name", "legal_name", "email", "telegram")
        ).lower()
        if search.lower() not in haystack:
            return False
    return True


async def _fx_autoload_loop(store: Store) -> None:
    while True:
        try:
            await asyncio.to_thread(store.autoload_reporting_rates)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("FX rate autoload failed")
        await asyncio.sleep(3600)


async def _notice_scheduler_loop(store: Store) -> None:
    """Send due notices every day at 11:15 GMT+4. Send logs keep deliveries idempotent."""
    while True:
        delay = seconds_until_notice_send()
        log.info("Next scheduled notices in %.0f seconds (11:15 GMT+4)", delay)
        await asyncio.sleep(delay)
        try:
            await asyncio.to_thread(send_due_notices, store)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Notice scheduler run failed")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_secrets()
    tasks: list[asyncio.Task] = []
    testing = bool(os.getenv("PYTEST_CURRENT_TEST"))
    if app.state.web_background_tasks and telegram_token() and not testing:
        from revshare.bot import poll_forever

        tasks.append(asyncio.create_task(poll_forever(app.state.store)))
    if app.state.web_background_tasks and app.state.store.fx_loader and not testing:
        tasks.append(asyncio.create_task(_fx_autoload_loop(app.state.store)))
    if (
        app.state.web_background_tasks
        and not testing
        and app.state.store.setting("notice_scheduler_enabled", "0") == "1"
    ):
        tasks.append(asyncio.create_task(_notice_scheduler_loop(app.state.store)))
    yield
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _invite_telegram_template(store, language: str) -> str:
    """Return the saved global invite_telegram template or the i18n default.

    The value shown in the settings page is the raw template (with
    ``{period}`` and ``{deadline}`` placeholders intact). The dispatcher
    formats it via ``revshare.notifications.format_notice`` at send time.
    """
    stored = store.setting(f"notice_text_{language}_invite_telegram", "")
    if stored.strip():
        return stored
    return translator(language)("invite_telegram_default")


def _session_actor(request: Request) -> str:
    """Best-effort actor identifier for audit log entries.

    The admin UI doesn't carry a real auth model today, so we record the
    ``user`` value from the signed cookie session when available and fall
    back to an empty string. This keeps audit rows readable when an
    enterprise SSO is wired up later without breaking the contract.
    """
    try:
        session = getattr(request, "session", None) or {}
    except Exception:
        session = {}
    value = session.get("user") if isinstance(session, dict) else ""
    return str(value) if value else ""


def create_app(
    db_path: Path | None = None,
    *,
    admin_auth: bool | None = None,
    public_forms_enabled: bool | None = None,
) -> FastAPI:
    load_secrets()
    app = FastAPI(
        title="Altegio Integration Hub Revshare",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.state.store = Store(
        db_path or Path(os.getenv("REVSHARE_DB", DEFAULT_DB)),
        fx_loader=fetch_month_average_rates if db_path is None else None,
    )
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    admin_user = os.getenv("REVSHARE_ADMIN_USER", "kate").strip()
    admin_password = os.getenv("REVSHARE_ADMIN_PASSWORD", "").strip()
    auth_override = os.getenv("REVSHARE_ADMIN_AUTH", "1").strip().lower()
    require_admin_auth = (
        db_path is None and auth_override not in {"0", "false", "no", "off"}
        if admin_auth is None
        else admin_auth
    )
    csrf_enabled = require_admin_auth
    app.state.web_background_tasks = (
        os.getenv("REVSHARE_WEB_BACKGROUND_TASKS", "0").strip() == "1"
    )
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.mcp_oauth_pending = {}
    # The browser based partner intake has been retired. Test apps may opt in
    # while preserving coverage for the historical parsing and review flow.
    public_forms_enabled = (
        db_path is not None if public_forms_enabled is None else public_forms_enabled
    )

    @app.middleware("http")
    async def protect_admin(request: Request, call_next):
        path = request.url.path
        if path.startswith("/submit/") and not public_forms_enabled:
            return PlainTextResponse(
                "Partner web form is no longer available. Please send the report through Telegram or contact the administrator.",
                status_code=410,
            )
        admin_path = (
            path == "/"
            or path.startswith("/admin/")
            or path.startswith("/reports/")
            or path.startswith("/export/")
        )
        if require_admin_auth and admin_path and not admin_password:
            log.error("Admin routes are disabled because REVSHARE_ADMIN_PASSWORD is missing")
            return PlainTextResponse(
                "Admin authentication is not configured",
                status_code=503,
            )
        if require_admin_auth and admin_password and admin_path:
            authorization = request.headers.get("authorization", "")
            valid = False
            if authorization.startswith("Basic "):
                try:
                    supplied = base64.b64decode(
                        authorization.removeprefix("Basic ")
                    ).decode("utf-8")
                    username, password = supplied.split(":", 1)
                    # hmac.compare_digest in Python 3.14 raises TypeError on
                    # non-ASCII strings; encode to latin-1 (1:1 byte mapping)
                    # so we can compare any UTF-8 input safely.
                    valid = hmac.compare_digest(
                        username.encode("latin-1", "replace"),
                        admin_user.encode("latin-1", "replace"),
                    ) and hmac.compare_digest(
                        password.encode("latin-1", "replace"),
                        admin_password.encode("latin-1", "replace"),
                    )
                except (binascii.Error, ValueError, UnicodeDecodeError):
                    valid = False
            if not valid:
                return PlainTextResponse(
                    "Authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Integration Hub Revshare"'},
                )
        if csrf_enabled and admin_path and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied_token = request.headers.get("x-csrf-token", "")
            if not supplied_token:
                # Fallback for non-JS clients that still submit csrf_token in the body.
                # Read the body once, validate the token, then put the raw bytes back
                # so FastAPI can re-parse the form in the route handler.
                body = await request.body()
                from urllib.parse import parse_qs

                parsed_form = parse_qs(body.decode("utf-8", errors="ignore"))
                supplied_token = (parsed_form.get("csrf_token") or [""])[0]
                if supplied_token:

                    async def receive_replay() -> dict[str, object]:
                        return {"type": "http.request", "body": body, "more_body": False}

                    request._receive = receive_replay  # type: ignore[attr-defined]
            if not hmac.compare_digest(supplied_token, app.state.csrf_token):
                return PlainTextResponse("Invalid CSRF token", status_code=403)
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.headers.get("origin")
            and request.headers["origin"] != f"{request.url.scheme}://{request.url.netloc}"
        ):
            return PlainTextResponse("Invalid request origin", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:;",
        )
        return response

    def current_language(request: Request) -> str:
        language = request.cookies.get("revshare_language", "en")
        return language if language in SUPPORTED_LANGUAGES else "en"

    def partner_page_language(request: Request, partner: dict) -> str:
        """Partner pages open in the language set on the partner card, until the visitor picks one."""
        chosen = request.cookies.get("revshare_language", "")
        if chosen in SUPPORTED_LANGUAGES:
            return chosen
        return partner["notice_settings"]["language"]

    def application_breakdown_with_usage(
        partner_id: int, period: str, items: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        application_ids = {
            item["application_id"]
            for item in items
            if isinstance(item.get("application_id"), int)
        }
        usage = {
            application_id: app.state.store.application_usage(
                partner_id, period, application_id
            )
            for application_id in application_ids
        }
        return add_usage_checks(items, usage)

    def _request_is_admin(request: Request) -> bool:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        if ":" not in decoded:
            return False
        username, _, password = decoded.partition(":")
        return hmac.compare_digest(username, admin_user) and hmac.compare_digest(
            password, admin_password
        )

    def _request_admin_user(request: Request) -> str:
        """Return the username from HTTP Basic auth, or "" when no auth header."""
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("basic "):
            return ""
        try:
            decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return ""
        if ":" not in decoded:
            return ""
        username, _, _password = decoded.partition(":")
        return username.strip()

    def _validate_base_period(
        request: Request, period: str
    ) -> tuple[str, RedirectResponse | None]:
        """Validate the ``period`` path argument.

        Returns ``(valid_period, None)`` on success or ``("", redirect_response)``
        on failure (the caller is expected to ``return error``).
        """
        selected = valid_period(period)
        if selected:
            return selected, None
        target = f"/?error=invalid_period&upload=reconciliation_base&next={period}"
        return "", RedirectResponse(target, status_code=303)

    def _render_base_form_error(
        request: Request,
        period: str,
        error_key: str,
        *,
        detail: str = "",
    ) -> HTMLResponse:
        existing = app.state.store.get_reconciliation_base(period)
        translated = translator(current_language(request))(error_key, detail=detail)
        return TEMPLATES.TemplateResponse(
            request,
            "admin_base_upload.html",
            context(
                request,
                period=period,
                base=existing,
                form_error=translated,
                success_count=0,
            ),
            status_code=400,
        )


    def context(request: Request, **values: object) -> dict:
        partner_page = "language_override" in values
        language = str(values.pop("language_override", "")) or current_language(request)
        selected_period = str(values.get("period") or current_period())
        path = request.url.path
        query = str(request.url.query)
        language_next = path + (f"?{query}" if query else "")
        admin_logged_in = _request_is_admin(request)
        return {
            "request": request,
            "current_period": current_period(),
            "period_options": recent_periods(selected_period),
            "lang": language,
            "partner_page": partner_page,
            "admin_logged_in": admin_logged_in,
            "ui_languages": SUPPORTED_LANGUAGES if partner_page else ADMIN_LANGUAGES,
            "language_labels": LANGUAGE_LABELS,
            "language_next": language_next,
            "t": translator(language),
            "csrf_token": app.state.csrf_token,
            "months_word": lambda count: months_word(language, int(count)),
            "countries": countries_for(language),
            "format_local": format_local_datetime,
            "format_amount": format_partner_amount,
            **values,
        }

    @app.get("/language/{language}")
    def set_language(language: str, next: str = "/"):
        if language not in SUPPORTED_LANGUAGES:
            raise HTTPException(404, "Language not supported")
        destination = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            "revshare_language",
            language,
            max_age=365 * 24 * 60 * 60,
            samesite="lax",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        period: str | None = None,
        status: str = "all",
        risk: str = "all",
        q: str = "",
        sort: str = "status_updated",
        dir: str = "desc",
    ):
        selected = valid_period(period) or current_period()
        items = app.state.store.dashboard(selected)
        counts = {
            "total": len(items),
            "submitted": sum(item["report_id"] is not None for item in items),
            "review": sum(item["status"] == "requires_review" for item in items),
            "missing": sum(item["report_id"] is None for item in items),
        }
        filters = dashboard_filters(status, risk, q)
        sort = sort if sort in DASHBOARD_SORTS else DEFAULT_SORT
        direction = "desc" if dir == "desc" else "asc"
        visible = sort_dashboard(
            [item for item in items if matches_dashboard_filters(item, filters)],
            sort,
            direction,
        )
        reported = [item for item in visible if item["report_id"]]
        verified_group = revshare_group(
            [item for item in reported if report_is_approved(item)],
            in_usd=True,
        )
        review_group = revshare_group(
            [item for item in reported if not report_is_approved(item)]
        )
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            context(
                request,
                period=selected,
                items=visible,
                total_items=len(items),
                counts=counts,
                filters=filters,
                filters_active=filters_are_active(filters),
                dashboard_statuses=DASHBOARD_STATUSES,
                dashboard_risks=DASHBOARD_RISKS,
                sort=sort,
                direction=direction,
                sort_links=sort_links(
                    {"period": selected, **filters}, sort, direction
                ),
                export_query=urlencode(
                    {"period": selected, **filters, "sort": sort, "dir": direction}
                ),
                turnover_total=sum(item["turnover"] or 0 for item in reported),
                revshare_total=sum(
                    item["expected_share"] or 0 for item in reported
                ),
                turnover_usd_total=sum(
                    item["turnover_usd"] or 0 for item in reported
                ),
                revshare_usd_total=sum(item["share_usd"] or 0 for item in reported),
                verified_group=verified_group,
                review_group=review_group,
                clients_total=sum(item["client_count"] or 0 for item in reported),
                counts_reported=len(reported),
                active_total=sum(item["active_count"] or 0 for item in visible),
                connected_total=sum(
                    item["connected_count"] or 0 for item in visible
                ),
                revshare_currencies=sorted(
                    {item["report_currency"] for item in reported}
                ),
            ),
        )

    @app.get("/admin/disconnect-candidates")
    def disconnect_candidates_page(period: str = ""):
        selected = valid_period(period) or current_period()
        return RedirectResponse(
            f"/admin/usage?period={selected}#disconnect-candidates",
            status_code=303,
        )

    @app.get("/admin/disconnect-candidates/{period}.csv")
    def export_disconnect_candidates(period: str):
        selected = valid_period(period)
        if not selected:
            raise HTTPException(404, "Invalid period")
        candidates = app.state.store.disconnect_candidates(selected)
        stream = io.StringIO()
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "Partner",
                "Altegio Location ID",
                "Idle months",
                "Idle since",
                "Reporting period",
            ]
        )
        for item in candidates:
            writer.writerow(
                [
                    item["partner_name"],
                    item["location_id"],
                    item["months"],
                    item["since"],
                    selected,
                ]
            )
        return StreamingResponse(
            iter([stream.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="disconnect-candidates-{selected}.csv"'
                )
            },
        )

    def refresh_usage_before_review(partner_id: int, period: str) -> bool:
        """Make a fresh Altegio comparison available for a new partner report."""
        if app.state.store.usage_import(partner_id, period) is not None:
            return True
        mcp_ready = bool(
            app.state.store.setting("mcp_access_token")
            or app.state.store.setting("mcp_refresh_token")
        )
        if not mysql_configured() and not mcp_ready:
            return False
        try:
            sync_period(app.state.store, period)
        except Exception:
            log.exception("Automatic Altegio check failed for period %s", period)
            return False
        return app.state.store.usage_import(partner_id, period) is not None

    def _load_connected_active_ids(period: str) -> tuple[set[str], set[str], bool]:
        """Return ``(connected, active, base_exists)`` for the ``assess`` call.

        Prefer the new reconciliation base rows table (the authoritative
        source). The third element of the tuple tells the caller whether a
        base exists — the partner flow needs this to switch into the
        ``pending_base`` branch.

        Returns ``base_exists=False`` when there is no reconciliation base,
        so callers can park the report until the admin uploads one. There
        is no fallback to the legacy ``usage_snapshots`` reads any more:
        ``altegio_sync`` is a no-op post-pivot and the legacy snapshot is
        no longer populated.
        """
        base_rows = app.state.store.get_reconciliation_base_rows(period)
        base_meta = app.state.store.get_reconciliation_base(period)
        if base_meta and base_rows:
            connected = {str(row["location_id"]) for row in base_rows}
            active = {
                str(row["location_id"]) for row in base_rows if int(row["active"])
            }
            return connected, active, True
        if base_meta:
            # A base metadata row exists but it has no rows. Treat it as
            # "base exists but is empty" — the partner flow stays open but
            # ``assess`` will return zero ids.
            return set(), set(), True
        # No base: the report is parked as ``pending_base`` until the admin
        # uploads one. No legacy snapshot fallback.
        return set(), set(), False

    @app.get("/submit/{token}", response_class=HTMLResponse)
    def submission_form(request: Request, token: str, period: str | None = None):
        from revshare.notifications import deadline_date

        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        selected = valid_period(period) or current_period()
        deadline = deadline_date(app.state.store.global_schedule(), selected)
        return TEMPLATES.TemplateResponse(
            request,
            "submit.html",
            context(
                request,
                language_override=partner_page_language(request, partner),
                partner=dict(partner),
                application_links=app.state.store.application_links(partner["id"]),
                period=selected,
                deadline_label=deadline.strftime("%d.%m.%Y"),
            ),
        )

    @app.get("/submit/{token}/template.xlsx")
    def report_template_xlsx(token: str, period: str | None = None):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        selected = valid_period(period) or current_period()
        content = build_report_template_xlsx(app.state.store, partner, selected)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="revshare-template-{selected}.xlsx"'},
        )

    @app.get("/submit/{token}/template.csv")
    def report_template(token: str, period: str | None = None):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        selected = valid_period(period) or current_period()
        _connected, active = app.state.store.usage(partner["id"], selected)
        stream = io.StringIO()
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "Application ID",
                "Application",
                "Location ID",
                "Turnover",
                "Currency",
                "Payment status",
                "Comment",
            ]
        )
        links = app.state.store.application_links(partner["id"])
        for link in links:
            app_id = parse_application_id(link.get("url", ""))
            usage = (
                app.state.store.application_usage(partner["id"], selected, app_id)
                if isinstance(app_id, int)
                else None
            )
            app_active = usage[1] if usage is not None else active
            for location_id in sorted(app_active):
                writer.writerow(
                    [
                        app_id or "",
                        link.get("comment", "") or link.get("url", ""),
                        location_id,
                        "",
                        partner["currency"],
                        "",
                        "",
                    ]
                )
        if not links:
            for location_id in sorted(active):
                writer.writerow(["", "", location_id, "", partner["currency"], "", ""])
        return StreamingResponse(
            iter([stream.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="revshare-template-{selected}.csv"'
            },
        )

    @app.post("/submit/{token}/parse")
    async def parse_report(
        request: Request,
        token: str,
        period: str = Form(...),
        attachment: UploadFile = File(...),
    ):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        if not attachment.filename:
            raise HTTPException(400, "Report file is required")
        selected_period = valid_period(period)
        if not selected_period:
            raise HTTPException(422, "Invalid reporting period")
        suffix = Path(attachment.filename).suffix.lower()
        if suffix not in {".xlsx", ".xlsm", ".xls", ".csv", ".pdf"}:
            raise HTTPException(400, "Поддерживаются Excel, CSV и PDF")
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_REPORT_UPLOAD_BYTES + 1024 * 1024:
            raise HTTPException(413, "Файл больше 10 МБ")
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(attachment.filename).name)
        attachment_name = f"{partner['id']}_{selected_period}_{secrets.token_hex(4)}_{safe_name}"
        content = await attachment.read(MAX_REPORT_UPLOAD_BYTES + 1)
        if len(content) > MAX_REPORT_UPLOAD_BYTES:
            raise HTTPException(400, "Файл больше 10 МБ")
        path = UPLOADS / attachment_name
        path.write_bytes(content)
        try:
            parsed = parse_partner_report(
                path,
                rate=partner["rate"],
                default_currency=partner["currency"],
                period=selected_period,
            )
        except Exception as error:
            path.unlink(missing_ok=True)
            raise HTTPException(400, "Не удалось прочитать файл отчёта") from error
        application_breakdown = breakdown_for_report(
            parsed, app.state.store.application_links(partner["id"])
        )
        draft_token = app.state.store.create_report_draft(
            partner_id=partner["id"],
            period=selected_period,
            attachment=attachment_name,
            turnover=parsed.turnover,
            currency=parsed.currency,
            reported_share=parsed.reported_share,
            location_ids=parsed.location_ids,
            extraction_notes=parsed.notes,
            application_breakdown=application_breakdown,
        )
        # If the hourly worker has not populated this month yet, run the same
        # read-only comparison now so the partner sees a real result straight
        # after uploading the file.
        refresh_usage_before_review(partner["id"], selected_period)
        return RedirectResponse(
            f"/submit/{token}/review/{draft_token}",
            status_code=303,
        )

    @app.get("/submit/{token}/review/{draft_token}", response_class=HTMLResponse)
    def review_report(
        request: Request,
        token: str,
        draft_token: str,
        error: str | None = None,
    ):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        draft = app.state.store.report_draft(draft_token, partner["id"])
        if not draft:
            raise HTTPException(404, "Draft not found")
        path = UPLOADS / draft["attachment"]
        report_reasons: dict[str, str] = {}
        report_reason_details: dict[str, str] = {}
        report_payment_statuses: dict[str, str] = {}
        report_status_details: dict[str, str] = {}
        report_all_ids = list(draft["location_ids"])
        report_has_status_fields = False
        if path.exists():
            parsed = parse_partner_report(
                path,
                rate=partner["rate"],
                default_currency=partner["currency"],
                period=draft["period"],
            )
            application_breakdown = breakdown_for_report(
                parsed, app.state.store.application_links(partner["id"])
            )
            report_reasons = parsed.report_reasons
            report_reason_details = parsed.report_reason_details
            report_payment_statuses = parsed.report_payment_statuses
            report_status_details = parsed.report_status_details
            report_all_ids = parsed.all_location_ids or list(parsed.location_ids)
            report_has_status_fields = "payment_status_column" in parsed.notes
            if (
                parsed.location_ids != draft["location_ids"]
                or parsed.turnover != draft["turnover"]
                or parsed.reported_share != draft["reported_share"]
                or application_breakdown != draft.get("application_breakdown", [])
            ):
                app.state.store.update_report_draft(
                    token=draft_token,
                    partner_id=partner["id"],
                    turnover=parsed.turnover,
                    currency=parsed.currency,
                    reported_share=parsed.reported_share,
                    location_ids=parsed.location_ids,
                    extraction_notes=parsed.notes,
                    application_breakdown=application_breakdown,
                )
                draft = app.state.store.report_draft(draft_token, partner["id"])
        snapshot_ready = refresh_usage_before_review(partner["id"], draft["period"])
        # Step 3: read connected/active from the new reconciliation base.
        # The partner flow uses ``base_exists`` to decide whether the
        # review screen should show the waiting-for-base branch.
        connected, active, base_exists = _load_connected_active_ids(
            draft["period"]
        )
        if not base_exists:
            # No base uploaded for this period yet. Skip the assessment
            # call (it would produce a misleading score) and let the
            # template render the waiting-for-base branch.
            check = assess([], connected, active)
        else:
            check = assess(draft["location_ids"], connected, active)
        reference_ids = active
        legacy_discrepancies = discrepancy_items(draft["location_ids"], active, active)
        payment_ids = {
            location_id
            for location_id, facts in app.state.store.usage_evidence(
                partner["id"], draft["period"]
            ).items()
            if facts["had_payment"]
        }
        application_breakdown = application_breakdown_with_usage(
            partner["id"], draft["period"], draft.get("application_breakdown", [])
        )
        application_check = application_registry_check(
            application_breakdown,
            app.state.store,
            partner["id"],
            draft["period"],
            report_payment_statuses,
        )
        discrepancies = (
            partner_registry_issues(
                set(report_all_ids), reference_ids, payment_ids, report_payment_statuses
            )
            if report_has_status_fields
            else legacy_discrepancies
        )
        # The application-level status check is only valid for the new
        # template with an explicit payment-status column. Older partner
        # files can still contain an application breakdown, but their zero
        # amounts are not a payment status and must not create 42 missing-
        # status errors.
        if report_has_status_fields and application_check is not None:
            discrepancies = application_check["issues"]
        long_idle = long_idle_locations(
            app.state.store, partner["id"], draft["period"], legacy_discrepancies
        )
        location_details = app.state.store.usage_location_details(
            partner["id"], draft["period"]
        )
        required = [item for item in discrepancies if item["required"]]
        missing_registry_count = sum(item["kind"] == "connected_not_reported" for item in required)
        missing_status_count = sum(
            item["kind"] in {"payment_status_missing", "payment_not_found"}
            for item in required
        )
        registry_gap_count = (
            missing_registry_count
            if report_has_status_fields
            else sum(item["kind"] == "active_not_reported" for item in required)
        )
        reasons_total = sum(
            1
            for location_id in set(report_all_ids) & reference_ids
            if report_has_status_fields and location_id not in payment_ids
        )
        reasons_filled = sum(
            1
            for location_id in set(report_all_ids) & reference_ids
            if report_has_status_fields and location_id not in payment_ids and report_payment_statuses.get(location_id)
        )
        report_rows = partner_report_rows(
            report_all_ids,
            set(draft["location_ids"]),
            connected,
            active,
            payment_ids,
            report_payment_statuses,
            report_status_details,
            location_details,
            application_names_by_location(application_breakdown),
        )
        payment_not_found_count = sum(
            item["kind"] == "payment_not_found" for item in required
        )
        missing_payment_status_count = sum(
            item["kind"] == "payment_status_missing" for item in required
        )
        effective_report_count = int(
            (application_check or {}).get("report_count", len(report_all_ids))
        )
        effective_registry_count = int(
            (application_check or {}).get("registry_count", len(reference_ids))
        )
        matched_count = max(
            0,
            min(effective_report_count, effective_registry_count)
            - payment_not_found_count
            - missing_payment_status_count,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            context(
                request,
                language_override=partner_page_language(request, partner),
                partner=dict(partner),
                draft=draft,
                check=check,
                connected_count=len(connected),
                active_count=len(active),
                snapshot_ready=snapshot_ready,
                # Step 3: pass the new ``base_exists`` flag to the template
                # so the review screen can switch into the waiting-for-base
                # branch when no reconciliation base has been uploaded yet.
                base_exists=base_exists,
                discrepancies=discrepancies,
                required_discrepancies=required,
                missing_registry_count=missing_registry_count,
                missing_status_count=missing_status_count,
                registry_gap_count=registry_gap_count,
                optional_discrepancies=[
                    item for item in discrepancies if not item["required"]
                ],
                application_breakdown=application_breakdown,
                long_idle=long_idle,
                location_details=location_details,
                report_reasons=report_reasons,
                report_reason_details=report_reason_details,
                report_payment_statuses=report_payment_statuses,
                report_status_details=report_status_details,
                report_has_status_fields=report_has_status_fields,
                report_all_ids=report_all_ids,
                report_count=effective_report_count,
                registry_count=effective_registry_count,
                payment_summary=partner_payment_summary(
                    set(report_all_ids), active, reference_ids, payment_ids, report_payment_statuses
                ),
                report_rows=report_rows,
                matched_count=matched_count,
                payment_not_found_count=payment_not_found_count,
                missing_payment_status_count=missing_payment_status_count,
                reasons_filled=reasons_filled,
                reasons_total=reasons_total,
                idle_months_threshold=app.state.store.idle_months_threshold(),
                form_error=error,
                active_tab=review_active_tab(
                    request, required_count=len(required)
                ),
            ),
        )

    @app.get("/submit/{token}/review/{draft_token}/full-report.csv")
    def download_full_report(token: str, draft_token: str):
        """Download a complete, editable account register for the partner."""
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Partner not found")
        draft = app.state.store.report_draft(draft_token, partner["id"])
        if not draft:
            raise HTTPException(404, "Draft not found")
        connected, active = app.state.store.usage(partner["id"], draft["period"])
        evidence = app.state.store.usage_evidence(partner["id"], draft["period"])
        payment_ids = {
            location_id for location_id, facts in evidence.items() if facts["had_payment"]
        }
        reported = set(draft["location_ids"])
        all_ids = sorted(active | reported)
        details = app.state.store.usage_location_details(partner["id"], draft["period"])
        existing_reasons: dict[str, str] = {}
        existing_details: dict[str, str] = {}
        existing_statuses: dict[str, str] = {}
        existing_status_details: dict[str, str] = {}
        path = UPLOADS / draft["attachment"]
        if path.exists():
            parsed = parse_partner_report(
                path,
                rate=partner["rate"],
                default_currency=partner["currency"],
                period=draft["period"],
            )
            existing_reasons = parsed.report_reasons
            existing_details = parsed.report_reason_details
            existing_statuses = parsed.report_payment_statuses
            existing_status_details = parsed.report_status_details
        application = ""
        application_rows: list[tuple[int | None, str, str, bool]] = []
        for item in draft.get("application_breakdown", []):
            application = str(item.get("application_name") or item.get("application_ref") or "")
            app_id = item.get("application_id")
            usage = (
                app.state.store.application_usage(partner["id"], draft["period"], app_id)
                if isinstance(app_id, int)
                else None
            )
            app_reference = set(item.get("location_ids") or [])
            app_all_ids = app_reference
            if usage is not None:
                _app_connected, app_active = usage
                app_all_ids |= app_active
            if not app_all_ids:
                app_all_ids = set(all_ids)
            application_rows.extend(
                (app_id if isinstance(app_id, int) else None, application, location_id, location_id in app_reference)
                for location_id in sorted(app_all_ids)
            )
        covered_ids = {location_id for _app_id, _application, location_id, _included in application_rows}
        application_rows.extend(
            (None, "", location_id, location_id in reported)
            for location_id in all_ids
            if location_id not in covered_ids
        )
        if not application_rows:
            application_rows = [(None, application, location_id, location_id in reported) for location_id in all_ids]
        seen_application_rows: set[tuple[int | None, str]] = set()
        stream = io.StringIO()
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "Application ID",
                "Application",
                "Location ID",
                "Название аккаунта",
                "Оборот",
                "Валюта",
                "Статус оплаты",
                "Комментарий",
            ]
        )
        for app_id, application, location_id, included in application_rows:
            row_key = (app_id, location_id)
            if row_key in seen_application_rows:
                continue
            seen_application_rows.add(row_key)
            detail = details.get(location_id, {})
            reason = existing_reasons.get(location_id, "")
            reason_detail = existing_details.get(location_id, "")
            status = existing_statuses.get(location_id, "")
            if not status and location_id in payment_ids:
                status = "paid"
            status_label = PAYMENT_STATUS_LABELS.get(status, "")
            status_detail = existing_status_details.get(location_id, "") or reason_detail
            writer.writerow(
                [
                    app_id or "",
                    application,
                    location_id,
                    detail.get("location_name") or "",
                    "",
                    draft["currency"] or partner["currency"],
                    status_label,
                    status_detail,
                ]
            )
        writer.writerow([])
        writer.writerow(["Итого оборот", "", "", "", draft["turnover"] or "", draft["currency"] or partner["currency"], "", ""])
        writer.writerow(["Итого revshare", "", "", "", draft["reported_share"] or "", draft["currency"] or partner["currency"], "", ""])
        return StreamingResponse(
            iter([stream.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="revshare-full-{draft["period"]}.csv"'
            },
        )

    def difference_csv_extra(
        details: dict[str, dict[str, object]], location_id: str
    ) -> list[str]:
        row = details.get(location_id, {})
        has_amounts = row.get("has_api_amounts")
        if has_amounts in (1, True):
            amounts = "Yes"
        elif has_amounts in (0, False):
            amounts = "No"
        else:
            amounts = ""
        return [
            str(row.get("location_name") or ""),
            str(row.get("connected_at") or ""),
            str(row.get("last_api_at") or ""),
            amounts,
        ]

    @app.get("/submit/{token}/review/{draft_token}/differences.csv")
    def download_draft_differences(token: str, draft_token: str, kind: str = "active"):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Partner not found")
        draft = app.state.store.report_draft(draft_token, partner["id"])
        if not draft:
            raise HTTPException(404, "Draft not found")
        connected, active = app.state.store.usage(partner["id"], draft["period"])
        location_details = app.state.store.usage_location_details(
            partner["id"], draft["period"]
        )
        reported = set(draft["location_ids"])
        if kind == "disconnect":
            discrepancies = discrepancy_items(draft["location_ids"], connected, active)
            rows = [
                (
                    candidate["location_id"],
                    f"Idle since {candidate['since']}, "
                    f"{candidate['months']} months in a row",
                )
                for candidate in long_idle_locations(
                    app.state.store, partner["id"], draft["period"], discrepancies
                )
            ]
            issue = "Missing from partner report"
        elif kind == "connected":
            rows = [
                (location_id, "Connected at month end, no API activity")
                for location_id in sorted((connected - active) - reported)
            ]
            issue = "Missing from partner report"
        else:
            rows = [
                (location_id, "Active during reporting month")
                for location_id in sorted(active - reported)
            ]
            issue = "Missing from partner report"
        stream = io.StringIO()
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "Altegio Location ID",
                "Reporting period",
                "Status in Altegio",
                "Issue",
                "Location name",
                "Connected at",
                "Last API activity",
                "Amounts via API",
            ]
        )
        for location_id, status in rows:
            writer.writerow(
                [
                    location_id,
                    draft["period"],
                    status,
                    issue,
                    *difference_csv_extra(location_details, location_id),
                ]
            )
        return StreamingResponse(
            iter([stream.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    "attachment; filename="
                    f'"altegio-{kind}-{draft["period"]}.csv"'
                )
            },
        )

    @app.post("/submit/{token}/confirm/{draft_token}", response_class=HTMLResponse)
    def confirm_report(
        request: Request,
        token: str,
        draft_token: str,
        comment: str = Form(""),
        claim_location: list[str] = Form([]),
        claim_kind: list[str] = Form([]),
        claim_reason: list[str] = Form([]),
        claim_detail: list[str] = Form([]),
        idle_declaration: str = Form(""),
    ):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        draft = app.state.store.report_draft(draft_token, partner["id"])
        if not draft:
            raise HTTPException(404, "Draft not found")
        ids = list(draft["location_ids"])
        share = draft["reported_share"]
        turnover = draft["turnover"]
        currency = (draft["currency"] or "USD").upper()
        # Step 3: the source of connected/active is the new reconciliation
        # base. ``submit_report`` below already routes to the
        # ``pending_base`` branch when no base is present.
        connected, active, _base_exists = _load_connected_active_ids(
            draft["period"]
        )
        report_all_ids = list(ids)
        file_reasons: dict[str, str] = {}
        file_reason_details: dict[str, str] = {}
        file_statuses: dict[str, str] = {}
        file_status_details: dict[str, str] = {}
        path = UPLOADS / draft["attachment"]
        if path.exists():
            parsed = parse_partner_report(
                path,
                rate=partner["rate"],
                default_currency=partner["currency"],
                period=draft["period"],
            )
            report_all_ids = parsed.all_location_ids or list(parsed.location_ids)
            file_reasons = parsed.report_reasons
            file_reason_details = parsed.report_reason_details
            file_statuses = parsed.report_payment_statuses
            file_status_details = parsed.report_status_details
        evidence = app.state.store.usage_evidence(partner["id"], draft["period"])
        payment_ids = {
            location_id for location_id, facts in evidence.items() if facts["had_payment"]
        }
        reference_ids = active
        legacy_discrepancies = discrepancy_items(ids, active, active)
        report_has_status_fields = "payment_status_column" in getattr(parsed, "notes", []) if path.exists() else False
        application_breakdown = application_breakdown_with_usage(
            partner["id"], draft["period"], draft.get("application_breakdown", [])
        )
        application_check = application_registry_check(
            application_breakdown,
            app.state.store,
            partner["id"],
            draft["period"],
            file_statuses,
        )
        discrepancies = (
            partner_registry_issues(
                set(report_all_ids), reference_ids, payment_ids, file_statuses
            )
            if report_has_status_fields
            else legacy_discrepancies
        )
        if report_has_status_fields and application_check is not None:
            discrepancies = application_check["issues"]
        long_idle = long_idle_locations(
            app.state.store, partner["id"], draft["period"], legacy_discrepancies
        )
        if app.state.store.usage_import(partner["id"], draft["period"]) is None:
            return RedirectResponse(
                f"/submit/{token}/review/{draft_token}?error=snapshot_missing",
                status_code=303,
            )
        submitted_claims = {
            (location, kind): {"reason": reason, "detail": detail.strip()}
            for location, kind, reason, detail in zip(
                claim_location, claim_kind, claim_reason, claim_detail
            )
        }
        claims = []
        claim_keys: set[tuple[str, str]] = set()

        def add_claim(item: dict[str, object], reason: str, detail: str = "") -> None:
            location_id = str(item["location_id"])
            kind = str(item.get("kind") or "active_not_reported")
            key = (location_id, kind)
            if key in claim_keys:
                return
            claim_keys.add(key)
            claims.append(
                {
                    **item,
                    "location_id": location_id,
                    "kind": kind,
                    "reason": reason,
                    "detail": detail,
                }
            )

        if not report_has_status_fields:
            for item in discrepancies:
                submitted = submitted_claims.get((item["location_id"], item["kind"]))
                if submitted and submitted["reason"] in CLAIM_REASON_KEYS:
                    if submitted["reason"] != "other" or submitted["detail"]:
                        add_claim(item, submitted["reason"], submitted["detail"])
                    continue
                if item["required"]:
                    add_claim(item, "unexplained")
        else:
            for item in discrepancies:
                if item["kind"] == "payment_not_found":
                    add_claim(
                        {**item, "kind": "active_not_reported"},
                        "unexplained",
                        "The file says that payment was received, but no Altegio payment was found.",
                    )
                elif item["kind"] in {"active_not_reported", "payment_status_missing"}:
                    location_id = str(item["location_id"])
                    reason = file_reasons.get(location_id, "")
                    detail = file_status_details.get(location_id, "") or file_reason_details.get(location_id, "")
                    normalized = {**item, "kind": "active_not_reported"}
                    if reason in CLAIM_REASON_KEYS and (reason != "other" or detail):
                        add_claim(normalized, reason, detail)
                    else:
                        add_claim(normalized, "unexplained")
        report_id, review = app.state.store.submit_report(
            partner_id=partner["id"],
            period=draft["period"],
            turnover=float(turnover or 0),
            currency=currency.upper(),
            reported_share=share,
            location_ids=ids,
            comment=comment,
            attachment=draft["attachment"],
            claims=claims,
            application_breakdown=draft.get("application_breakdown", []),
        )
        app.state.store.delete_report_draft(draft_token, partner["id"])
        return TEMPLATES.TemplateResponse(
            request,
            "submitted.html",
            context(
                request,
                language_override=partner_page_language(request, partner),
                partner=dict(partner),
                period=draft["period"],
                report_id=report_id,
                review=review,
                file_ids=len(ids),
                file_source="column" if ids else "not_found",
                discrepancy_count=len([item for item in discrepancies if item["required"]]),
            ),
        )

    @app.get("/admin/reconciliation-base/{period}", response_class=HTMLResponse)
    def reconciliation_base_form(request: Request, period: str):
        valid, error = _validate_base_period(request, period)
        if not valid:
            return error  # type: ignore[return-value]
        existing = app.state.store.get_reconciliation_base(valid)
        # Step 3: the upload page polls the revalidation task started by
        # the POST. Prefer the fresh task id from the redirect query
        # (``?task_id=...``) so the user always sees the progress bar
        # for the upload they just performed; otherwise pick up the most
        # recent in-flight task for this period (a refresh, an admin
        # background job, etc.).
        task_id = (request.query_params.get("task_id") or "").strip()
        if not task_id:
            active_tasks = app.state.store.list_active_revalidation_tasks(valid)
            if active_tasks:
                task_id = active_tasks[0]["id"]
        return TEMPLATES.TemplateResponse(
            request,
            "admin_base_upload.html",
            context(
                request,
                period=valid,
                base=existing,
                form_error=request.query_params.get("error") or None,
                success_count=int(request.query_params.get("n") or 0),
                revalidation_task_id=task_id,
            ),
        )

    @app.get(
        "/admin/reconciliation-base/revalidation-status/{task_id}",
        response_class=Response,
    )
    def revalidation_status(task_id: str):
        """Return the live progress JSON for the revalidation worker.

        404 when the task id is unknown. The endpoint is admin-only via
        the surrounding ``require_admin_auth`` gate; partners cannot hit
        it because no public route maps here.
        """
        row = app.state.store.get_revalidation_task(task_id)
        if row is None:
            raise HTTPException(404, "Revalidation task not found")
        payload = {
            "task_id": row["id"],
            "period": row["period"],
            "status": row["status"],
            "total": int(row["total"]),
            "processed": int(row["processed"]),
            "current_report": str(row["current_report"] or ""),
            "summary": row.get("summary") or {},
            "started_at": str(row["started_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False),
            media_type="application/json",
        )

    @app.post("/admin/reconciliation-base/{period}")
    async def reconciliation_base_upload(
        request: Request,
        period: str,
        base_file: UploadFile | None = File(None),
    ):
        valid, error = _validate_base_period(request, period)
        if not valid:
            return error  # type: ignore[return-value]
        if not base_file or not base_file.filename:
            return _render_base_form_error(
                request, valid, "reconciliation_base_error_wrong_extension"
            )
        suffix = Path(base_file.filename).suffix.lower()
        if suffix not in {".csv", ".xlsx"}:
            return _render_base_form_error(
                request, valid, "reconciliation_base_error_wrong_extension"
            )
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(base_file.filename).name)
        content = await base_file.read(MAX_BASE_UPLOAD_BYTES + 1)
        if len(content) > MAX_BASE_UPLOAD_BYTES:
            return _render_base_form_error(
                request, valid, "reconciliation_base_error_too_large"
            )
        target_dir = UPLOADS / "reconciliation_base"
        target_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{valid}-{secrets.token_hex(4)}{suffix}"
        path = target_dir / stored_name
        path.write_bytes(content)
        try:
            parsed = parse_reconciliation_base(path)
        except Exception as exc:  # noqa: BLE001 — surface parser issues, never crash
            path.unlink(missing_ok=True)
            return _render_base_form_error(
                request,
                valid,
                "reconciliation_base_error_parser",
                detail=str(exc),
            )
        if parsed.error:
            path.unlink(missing_ok=True)
            return _render_base_form_error(
                request,
                valid,
                "reconciliation_base_error_parser",
                detail=parsed.error,
            )
        if not parsed.rows:
            path.unlink(missing_ok=True)
            return _render_base_form_error(
                request, valid, "reconciliation_base_error_empty"
            )
        rows: list[BaseRow] = list(parsed.rows)
        uploaded_by = _request_admin_user(request) or admin_user
        relative_path = f"reconciliation_base/{stored_name}"
        app.state.store.replace_reconciliation_base(
            period=valid,
            uploaded_at=utc_now_iso(),
            uploaded_by=uploaded_by,
            file_path=relative_path,
            rows=rows,
            notes="\n".join(parsed.notes),
        )
        # Step 3: every existing report for this period must be re-evaluated
        # against the freshly loaded base. Schedule a background worker and
        # pass the task id back so the upload page can poll its progress.
        task_id = schedule_revalidation(app.state.store, period=valid)
        return RedirectResponse(
            (
                f"/admin/reconciliation-base/{valid}"
                f"?saved=1&n={len(rows)}&task_id={task_id}"
            ),
            status_code=303,
        )

    @app.get("/admin/usage", response_class=HTMLResponse)
    def usage_form(request: Request, partner_id: int | None = None, period: str | None = None):
        partners = [dict(row) for row in app.state.store.partners()]
        selected_id = partner_id or (partners[0]["id"] if partners else None)
        selected_period = valid_period(period) or current_period()
        connected: set[str] = set()
        active: set[str] = set()
        payment: set[str] = set()
        if selected_id:
            connected, active = app.state.store.usage(selected_id, selected_period)
            evidence = app.state.store.usage_evidence(selected_id, selected_period)
            payment = {
                location_id
                for location_id, facts in evidence.items()
                if facts["had_payment"]
            }
        report_id = (
            app.state.store.report_id_for(selected_id, selected_period)
            if selected_id
            else None
        )
        return TEMPLATES.TemplateResponse(
            request,
            "usage.html",
            context(
                request,
                partners=partners,
                partner_id=selected_id,
                period=selected_period,
                connected="\n".join(sorted(connected)),
                active="\n".join(sorted(active)),
                payment="\n".join(sorted(payment)),
                connected_count=len(connected),
                active_count=len(active),
                payment_count=len(payment),
                report_id=report_id,
                usage_import=app.state.store.usage_import(
                    selected_id, selected_period
                )
                if selected_id
                else None,
                candidates=app.state.store.disconnect_candidates(selected_period),
                idle_months_threshold=app.state.store.idle_months_threshold(),
            ),
        )

    @app.post("/admin/usage")
    def save_usage(
        partner_id: int = Form(...),
        period: str = Form(...),
        connected_ids: str = Form(""),
        active_ids: str = Form(""),
        payment_ids: str = Form(""),
    ):
        selected_period = valid_period(period)
        if not selected_period:
            raise HTTPException(422, "Invalid reporting period")
        app.state.store.replace_usage(
            partner_id,
            selected_period,
            normalize_ids(connected_ids),
            normalize_ids(active_ids),
            normalize_ids(payment_ids),
        )
        report_id = app.state.store.report_id_for(partner_id, selected_period)
        destination = (
            f"/reports/{report_id}"
            if report_id
            else f"/admin/usage?partner_id={partner_id}&period={selected_period}"
        )
        return RedirectResponse(destination, status_code=303)

    def _import_snapshot(
        period: str, attachment: UploadFile | None = None
    ) -> RedirectResponse:
        selected = valid_period(period) or current_period()
        try:
            if attachment and attachment.filename:
                raw = attachment.file.read(MAX_SNAPSHOT_UPLOAD_BYTES + 1)
                if len(raw) > MAX_SNAPSHOT_UPLOAD_BYTES:
                    raise ValueError("snapshot_too_large")
                payload = json.loads(raw.decode("utf-8"))
                result = apply_snapshot(
                    app.state.store, payload, source="json", default_period=selected
                )
            else:
                path = local_snapshot_path(selected)
                if path.exists():
                    result = apply_snapshot(
                        app.state.store,
                        load_snapshot_file(path),
                        source="json",
                        default_period=selected,
                    )
                else:
                    result = apply_snapshot(
                        app.state.store,
                        fetch_snapshot(selected),
                        source="api",
                        default_period=selected,
                    )
        except Exception:
            log.exception("Altegio snapshot import failed")
            return RedirectResponse(
                f"/admin/settings?period={selected}&import=error#altegio",
                status_code=303,
            )
        return RedirectResponse(
            f"/admin/settings?period={selected}&imported={result['imported']}#altegio",
            status_code=303,
        )

    @app.post("/admin/settings/altegio/sync")
    def sync_altegio_data(period: str = Form(...)):
        selected = valid_period(period) or current_period()
        try:
            result = sync_period(app.state.store, selected)
        except RuntimeError as error:
            if str(error) == "altegio_mysql_missing":
                return RedirectResponse(
                    f"/admin/settings?period={selected}&sync=missing#altegio",
                    status_code=303,
                )
            return RedirectResponse(
                f"/admin/settings?period={selected}&sync=error#altegio",
                status_code=303,
            )
        except Exception:
            log.exception("Altegio MySQL sync failed")
            return RedirectResponse(
                f"/admin/settings?period={selected}&sync=error#altegio",
                status_code=303,
            )
        return RedirectResponse(
            (
                f"/admin/settings?period={selected}&synced={result.partners_synced}"
                f"&skipped={len(result.partners_skipped)}#altegio"
            ),
            status_code=303,
        )

    @app.post("/admin/settings/altegio/import")
    def import_altegio_snapshot(
        period: str = Form(...),
        attachment: UploadFile | None = File(None),
    ):
        return _import_snapshot(period, attachment)

    @app.get("/admin/accrual", response_class=HTMLResponse)
    def accrual_page(request: Request, period: str = "", queued: str = ""):
        selected = valid_period(period) or current_period()
        queued_count = int(queued) if queued.isdigit() else 0
        sheet_ready = sheets.credentials_ready()
        flushed_count = int(request.query_params.get("flushed") or 0)
        return TEMPLATES.TemplateResponse(
            request,
            "accrual.html",
            context(
                request,
                period=selected,
                items=app.state.store.accrual_queue(selected),
                writes=app.state.store.sheet_writes(selected),
                sheet_ready=sheet_ready,
                queued_count=queued_count,
                flushed_count=flushed_count,
            ),
        )

    @app.post("/admin/accrual/queue")
    def queue_accrual_reports(
        period: str = Form(...),
        report_ids: list[int] = Form(default=[]),
    ):
        selected = valid_period(period) or current_period()
        if not report_ids:
            return RedirectResponse(f"/admin/accrual?period={selected}", status_code=303)
        ready_ids = {
            item["report_id"]
            for item in app.state.store.accrual_queue(selected)
            if item["report_id"]
        }
        allowed = [report_id for report_id in report_ids if report_id in ready_ids]
        count = app.state.store.mark_posted_many(
            allowed,
            "Queued for Partnerships Altegio Revenue Accrual.",
        )
        if sheets.credentials_ready():
            flush_queued_sheet_writes(app.state.store)
        query = urlencode({"period": selected, "queued": count})
        return RedirectResponse(f"/admin/accrual?{query}", status_code=303)

    @app.post("/admin/accrual/flush")
    def flush_accrual_queue(period: str = Form("")):
        selected = valid_period(period) or current_period()
        app.state.store.requeue_failed_sheet_writes(selected)
        results = flush_queued_sheet_writes(app.state.store)
        return RedirectResponse(
            f"/admin/accrual?period={selected}&flushed={len(results)}",
            status_code=303,
        )

    @app.get("/admin/partners", response_class=HTMLResponse)
    def partners_page(request: Request, view: str = ""):
        archived = view == "archived"
        source = app.state.store.archived_partners() if archived else app.state.store.partners()
        partners = [dict(row) for row in source]
        for partner in partners:
            partner["application_categories"] = sorted(
                {
                    item["category"]
                    for item in app.state.store.application_links(partner["id"])
                    if item["category"]
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "partners.html",
            context(
                request,
                partners=partners,
                archived=archived,
                archived_count=len(app.state.store.archived_partners()),
            ),
        )

    @app.post("/admin/partners")
    def add_partner(
        name: str = Form(""),
        legal_name: str = Form(""),
        rate: str = Form(""),
    ):
        token = secrets.token_urlsafe(18)
        try:
            rate_value = float(rate) / 100 if rate.strip() else None
        except ValueError as error:
            raise HTTPException(422, "Rate must be a number") from error
        if rate_value is not None and not 0 <= rate_value <= 1:
            raise HTTPException(422, "Rate must be between 0 and 100 percent")
        partner_name = name.strip() or f"Новый партнёр {token[-6:]}"
        app.state.store.add_partner(
            name=partner_name,
            legal_name=legal_name.strip(),
            legal_country="",
            altegio_legal_entity="",
            contact_name="",
            email="",
            telegram="",
            report_channel="telegram",
            token=token,
            rate=rate_value,
            currency="USD",
        )
        return RedirectResponse("/admin/partners", status_code=303)

    @app.get("/admin/partners/{partner_id}/edit", response_class=HTMLResponse)
    def edit_partner_page(
        request: Request,
        partner_id: int,
        tab: str = "",
        saved: str = "",
        view: str = "",
        deleted: str = "",
    ):
        partner = app.state.store.partner(partner_id)
        if not partner:
            raise HTTPException(404, "Partner not found")
        archived = not bool(partner["active"])
        payload = dict(partner)
        payload["legal_country"] = country_label(
            payload["legal_country"], current_language(request)
        )
        settings = payload["notice_settings"]
        history = app.state.store.partner_history(partner_id)
        # History tab can show either confirmed reports (default) or
        # bot-created drafts waiting for partner confirmation.
        history_view = view if view in ("reports", "drafts") else "reports"
        drafts = (
            app.state.store.drafts_for_partner(partner_id)
            if history_view == "drafts"
            else []
        )
        bot_username = (
            app.state.store.setting("telegram_bot_username")
            or os.getenv("TELEGRAM_BOT_USERNAME", "")
        )
        # Show the freshly-created invite token after a contact change so the
        # admin can copy it. We never read it back from the DB on subsequent
        # visits — only the token_prefix is stored, which is enough to show
        # "an active invite exists" without leaking the secret.
        new_invite_token = request.query_params.get("tg_invite_token", "")
        active_invite = app.state.store.active_telegram_invite(partner_id)
        tg_changed = request.query_params.get("tg_changed") == "1"
        tg_unlinked = request.query_params.get("tg_unlinked") == "1"
        tg_inherited = request.query_params.get("tg_inherited") == "1"
        return TEMPLATES.TemplateResponse(
            request,
            "partner_edit.html",
            context(
                request,
                partner=payload,
                archived=archived,
                history=history,
                drafts=drafts,
                history_view=history_view,
                history_chart=history_chart(history),
                application_links=app.state.store.application_links(partner_id),
                altegio_legal_entities=ALTEGIO_LEGAL_ENTITIES,
                app_categories=APP_CATEGORIES,
                notice_languages=NOTICE_LANGUAGES,
                notice_settings=settings,
                planned_dates={
                    item["key"]: item["send_on"].isoformat()
                    for item in schedule_for_period(
                        settings,
                        current_period(),
                        name=payload["name"],
                        link="",
                    )
                },
                bot_username=bot_username,
                bot_configured=bool(telegram_token()),
                active_invite=active_invite,
                new_invite_token=new_invite_token,
                tg_changed=tg_changed,
                tg_unlinked=tg_unlinked,
                tg_inherited=tg_inherited,
                notice_result=request.query_params.get("notice"),
                notice_reason=request.query_params.get("reason"),
                active_tab=tab if tab in PARTNER_TABS else PARTNER_TABS[0],
                partner_tabs=PARTNER_TABS,
                saved=saved == "1",
                unlinked=request.query_params.get("unlinked") == "1",
                draft_deleted=deleted == "1" or request.query_params.get("deleted") == "1",
            ),
        )

    @app.post("/admin/partners/{partner_id}/drafts/{draft_id}/delete")
    def delete_partner_draft(partner_id: int, draft_id: int):
        """Hard-delete a bot-created draft from the admin profile page.

        Only deletes the draft if it belongs to this partner — guards
        against accidentally removing someone else's draft by guessing
        an id. The CSRF middleware already protects this POST.
        """
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        deleted = app.state.store.delete_draft(draft_id, partner_id)
        if not deleted:
            raise HTTPException(404, "Draft not found for this partner")
        return RedirectResponse(
            f"/admin/partners/{partner_id}/edit?tab=history&view=drafts&deleted=1",
            status_code=303,
        )

    @app.get("/admin/partners/{partner_id}/report", response_class=HTMLResponse)
    def manual_report_page(
        request: Request, partner_id: int, period: str = ""
    ):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        return TEMPLATES.TemplateResponse(
            request,
            "manual_report.html",
            context(
                request,
                partner=dict(partner),
                period=valid_period(period) or current_period(),
                currency=partner["currency"] or "USD",
                currencies=SUPPORTED_CURRENCIES,
            ),
        )

    @app.post("/admin/partners/{partner_id}/report")
    def create_manual_report(
        partner_id: int,
        period: str = Form(""),
        turnover: str = Form(""),
        currency: str = Form("USD"),
        reported_share: str = Form(""),
        source_note: str = Form(""),
        comment: str = Form(""),
    ):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        selected_period = valid_period(period)
        if not selected_period:
            raise HTTPException(422, "Invalid reporting period")
        normalized_currency = currency.upper().strip()
        if normalized_currency not in SUPPORTED_CURRENCIES:
            raise HTTPException(422, "Unsupported currency")
        try:
            turnover_value = float(turnover.replace(",", "."))
            share_value = (
                float(reported_share.replace(",", "."))
                if reported_share.strip()
                else None
            )
        except ValueError as error:
            raise HTTPException(422, "Amounts must be numbers") from error
        if turnover_value < 0 or (share_value is not None and share_value < 0):
            raise HTTPException(422, "Amounts cannot be negative")
        source = source_note.strip()
        note = comment.strip()
        if source:
            note = f"{source}\n{note}".strip()
        report_id, _ = app.state.store.submit_manual_report(
            partner_id=partner_id,
            period=selected_period,
            turnover=turnover_value,
            currency=normalized_currency,
            reported_share=share_value,
            comment=note,
        )
        return RedirectResponse(f"/reports/{report_id}", status_code=303)

    @app.post("/admin/partners/{partner_id}/edit")
    def edit_partner(
        request: Request,
        partner_id: int,
        name: str = Form(""),
        legal_name: str = Form(""),
        legal_country: str = Form(""),
        altegio_legal_entity: str = Form(""),
        contact_name: str = Form(""),
        telegram: str = Form(""),
        internal_comment: str = Form(""),
        application_url: list[str] = Form(default=[]),
        application_comment: list[str] = Form(default=[]),
        application_category: list[str] = Form(default=[]),
        rate: str = Form(""),
        currency: str = Form("USD"),
        notice_language: str = Form("en"),
        notice_on: list[str] = Form(default=[]),
        notice_key: list[str] = Form(default=[]),
        notice_day: list[str] = Form(default=[]),
        own_schedule: str = Form(""),
        report_automation_status: str = Form(""),
        tab: str = "",
    ):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        altegio_legal_entity = altegio_legal_entity.strip()
        if altegio_legal_entity and altegio_legal_entity not in ALTEGIO_LEGAL_ENTITIES:
            raise HTTPException(422, "Invalid Altegio legal entity")
        if report_automation_status not in PARTNER_AUTOMATION_STATUSES:
            report_automation_status = str(
                partner["report_automation_status"] or "manual"
            )
        if currency.upper() not in SUPPORTED_CURRENCIES:
            raise HTTPException(422, "Unsupported currency")
        try:
            links = normalize_application_links(
                application_url, application_comment, application_category
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        notice_settings = None
        if notice_key:
            notice_settings = settings_from_form(
                weekend_shift=True,
                enabled_keys=notice_on,
                days=dict(zip(notice_key, notice_day)),
                messages=app.state.store.notice_templates(notice_language),
                language=notice_language,
                schedule_override=own_schedule in {"on", "true", "1"},
                global_schedule=app.state.store.global_schedule(),
            )
        try:
            rate_value = float(rate) / 100 if rate.strip() else None
        except ValueError as error:
            raise HTTPException(422, "Rate must be a number") from error
        if rate_value is not None and not 0 <= rate_value <= 1:
            raise HTTPException(422, "Rate must be between 0 and 100 percent")
        partner_name = name.strip() or f"Новый партнёр {partner_id}"
        app.state.store.update_partner(
            partner_id=partner_id,
            name=partner_name,
            legal_name=legal_name.strip(),
            legal_country=canonical_country(legal_country),
            altegio_legal_entity=altegio_legal_entity,
            contact_name=contact_name.strip(),
            email=partner["email"] or "",
            # Telegram handle is intentionally NOT passed: the contact-change
            # flow below reads the previous value, compares it to the new one,
            # and writes the new handle only when it actually changed (with
            # the chat_id unlink and invite revoke in the same transaction).
            report_channel="telegram",
            rate=rate_value,
            currency=currency.upper(),
            notice_settings=notice_settings,
            category=links[0]["category"] if links else "",
            internal_comment=internal_comment.strip(),
            report_automation_status=report_automation_status,
        )
        app.state.store.replace_application_links(partner_id, links)
        bot_configured = bool(telegram_token())
        create_invite = bool(telegram.strip()) and bot_configured
        contact_result = app.state.store.apply_telegram_contact_change(
            partner_id,
            telegram.strip(),
            actor=_session_actor(request),
            bot_configured=bot_configured,
            create_invite=create_invite,
        )
        params = {"saved": "1"}
        if tab in PARTNER_TABS:
            params["tab"] = tab
        if contact_result.get("handle_changed"):
            params["tg_changed"] = "1"
            if contact_result.get("unlinked_chat_id"):
                params["tg_unlinked"] = "1"
            invite = contact_result.get("invite")
            if invite is not None:
                params["tg_invite"] = "1"
                # Pass the token only this once via the redirect so the
                # admin can copy it. Subsequent reloads won't see the
                # token (it's not stored in a retrievable form).
                params["tg_invite_token"] = invite["token"]
            elif contact_result.get("inherited_chat_id"):
                params["tg_inherited"] = "1"
        return RedirectResponse(
            f"/admin/partners/{partner_id}/edit?{urlencode(params)}",
            status_code=303,
        )

    @app.post("/admin/partners/{partner_id}/notifications")
    def save_partner_notifications(
        partner_id: int,
        notice_language: str = Form("en"),
        notice_on: list[str] = Form(default=[]),
        notice_key: list[str] = Form(default=[]),
        notice_day: list[str] = Form(default=[]),
        own_schedule: str = Form(""),
    ):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        app.state.store.set_partner_notifications(
            partner_id,
            settings_from_form(
                weekend_shift=True,
                enabled_keys=notice_on,
                days=dict(zip(notice_key, notice_day)),
                messages=app.state.store.notice_templates(notice_language),
                language=notice_language,
                schedule_override=own_schedule in {"on", "true", "1"},
                global_schedule=app.state.store.global_schedule(),
            ),
        )
        return RedirectResponse(
            f"/admin/partners/{partner_id}/edit?tab=settings&saved=1",
            status_code=303,
        )

    @app.post("/admin/partners/{partner_id}/notify")
    def send_partner_now(partner_id: int, notice_key: str = Form("invite")):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        result = send_partner_notice(
            app.state.store,
            partner,
            current_period(),
            notice_key,
            force=True,
        )
        return RedirectResponse(
            f"/admin/partners/{partner_id}/edit"
            f"?notice={result['status']}&reason={result['detail']}",
            status_code=303,
        )

    @app.get("/admin/settings/mcp/connect")
    def connect_altegio_mcp(request: Request):
        redirect_uri = str(request.base_url).rstrip("/") + "/admin/settings/mcp/callback"
        try:
            client_id = register_client(redirect_uri)
        except MCPError:
            return RedirectResponse("/admin/settings?mcp=error#altegio", status_code=303)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        app.state.mcp_oauth_pending[state] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "verifier": verifier,
        }
        return RedirectResponse(
            authorization_url(client_id, redirect_uri, state, challenge),
            status_code=303,
        )

    @app.get("/admin/settings/mcp/callback")
    def altegio_mcp_callback(
        code: str = "", state: str = "", error: str = ""
    ):
        pending = app.state.mcp_oauth_pending.pop(state, None)
        if error or not pending or not code:
            return RedirectResponse("/admin/settings?mcp=denied#altegio", status_code=303)
        try:
            payload = exchange_code(
                client_id=pending["client_id"],
                code=code,
                redirect_uri=pending["redirect_uri"],
                verifier=pending["verifier"],
            )
            store_token_payload(app.state.store, pending["client_id"], payload)
        except MCPError:
            return RedirectResponse("/admin/settings?mcp=error#altegio", status_code=303)
        return RedirectResponse("/admin/settings?mcp=connected#altegio", status_code=303)

    SETTINGS_TABS = ("bot", "schedule", "notices")

    @app.get("/admin/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request,
        language: str = "",
        saved: str = "",
        period: str = "",
        tab: str = "",
    ):
        selected = language if language in NOTICE_LANGUAGES else current_language(request)
        if selected not in NOTICE_LANGUAGES:
            selected = "en"
        active_tab = tab if tab in SETTINGS_TABS else "bot"
        global_schedule = app.state.store.global_schedule()
        planned = {
            item["key"]: item["send_on"].isoformat()
            for item in schedule_for_period(
                {
                    "language": selected,
                    "schedule_override": True,
                    "notices": global_schedule["notices"],
                },
                current_period(),
            )
        }
        rates_period = valid_period(period) or current_period()
        altegio_period = rates_period
        fx = app.state.store.fx_snapshot(rates_period)
        query = request.query_params
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            context(
                request,
                language=selected,
                period=rates_period,
                altegio_period=altegio_period,
                import_count=query.get("imported"),
                import_error=query.get("import") == "error",
                sync_missing=query.get("sync") == "missing",
                sync_error=query.get("sync") == "error",
                sync_done=int(query.get("synced") or 0),
                sync_skipped=int(query.get("skipped") or 0),
                mcp_connected=bool(
                    app.state.store.setting("mcp_refresh_token")
                    or app.state.store.setting("mcp_access_token")
                ),
                mcp_status=query.get("mcp", ""),
                altegio_mysql_ready=mysql_configured(),
                altegio_data_ready=mysql_configured()
                or bool(
                    app.state.store.setting("mcp_refresh_token")
                    or app.state.store.setting("mcp_access_token")
                ),
                snapshot_api_ready=bool(snapshot_url()),
                notice_languages=NOTICE_LANGUAGES,
                partner_notice_keys=PARTNER_NOTICE_KEYS,
                notice_keys=NOTICE_KEYS,
                templates=app.state.store.notice_templates(selected),
                invite_telegram_template=_invite_telegram_template(
                    app.state.store, selected
                ),
                fx_currencies=SUPPORTED_CURRENCIES,
                fx_rates=fx["rates"],
                fx_info=fx,
                idle_months_threshold=app.state.store.idle_months_threshold(),
                global_schedule=global_schedule,
                schedule_dates=planned,
                saved=saved == "1",
                bot_ready=bool(telegram_token()),
                bot_username=app.state.store.setting("telegram_bot_username")
                or os.getenv("TELEGRAM_BOT_USERNAME", ""),
                digest_linked=bool(app.state.store.setting("kate_telegram_chat_id")),
                digest_chat=app.state.store.setting("digest_chat_id"),
                notices_ran=request.query_params.get("notice") == "ran",
                notice_scheduler_enabled=app.state.store.setting(
                    "notice_scheduler_enabled", "0"
                )
                == "1",
                active_tab=active_tab,
            ),
        )

    @app.get("/admin/notices", response_class=HTMLResponse)
    def notice_texts_redirect(language: str = "", saved: str = ""):
        params = []
        if language:
            params.append(f"language={language}")
        if saved:
            params.append("saved=1")
        suffix = ("?" + "&".join(params)) if params else ""
        return RedirectResponse(f"/admin/settings{suffix}", status_code=303)

    @app.post("/admin/settings/altegio")
    def save_altegio_settings(
        idle_months_threshold: str = Form("3"),
        period: str = Form(""),
    ):
        try:
            threshold = int(idle_months_threshold)
        except ValueError as error:
            raise HTTPException(422, "Idle months threshold must be a number") from error
        if not 1 <= threshold <= 120:
            raise HTTPException(422, "Idle months threshold must be between 1 and 120")
        app.state.store.set_idle_months_threshold(str(threshold))
        selected = valid_period(period) or current_period()
        return RedirectResponse(
            f"/admin/settings?period={selected}&saved=1#altegio", status_code=303
        )

    @app.post("/admin/settings/telegram")
    def save_digest_chat(digest_chat: str = Form("")):
        app.state.store.set_digest_chat(digest_chat)
        return RedirectResponse("/admin/settings?saved=1&tab=digest", status_code=303)

    @app.post("/admin/settings/schedule")
    def save_global_schedule(
        notice_on: list[str] = Form(default=[]),
        notice_key: list[str] = Form(default=[]),
        notice_day: list[str] = Form(default=[]),
        notice_scheduler_enabled: str = Form(""),
    ):
        app.state.store.set_global_schedule(
            notice_on, dict(zip(notice_key, notice_day))
        )
        app.state.store.set_setting(
            "notice_scheduler_enabled",
            "1" if notice_scheduler_enabled in {"on", "true", "1"} else "0",
        )
        return RedirectResponse("/admin/settings?saved=1&tab=schedule", status_code=303)

    @app.post("/admin/rates")
    async def save_fx_rates(request: Request):
        form = await request.form()
        period = valid_period(str(form.get("period") or "")) or current_period()
        app.state.store.set_fx_rates(
            {
                key.removeprefix("rate_"): str(value)
                for key, value in form.items()
                if key.startswith("rate_")
            },
            period=period,
        )
        return RedirectResponse(
            f"/admin/settings?period={period}&saved=1#rates", status_code=303
        )

    @app.post("/admin/rates/refresh")
    def refresh_fx_rates(period: str = Form("")):
        rates_period = valid_period(period) or current_period()
        app.state.store.fx_snapshot(rates_period, refresh=True)
        return RedirectResponse(
            f"/admin/settings?period={rates_period}&saved=1#rates", status_code=303
        )

    @app.post("/admin/notices")
    def save_notice_texts(
        language: str = Form("en"),
        notice_key: list[str] = Form(default=[]),
        notice_message: list[str] = Form(default=[]),
    ):
        if language not in NOTICE_LANGUAGES:
            raise HTTPException(422, "Language not supported")
        app.state.store.set_notice_templates(
            language, dict(zip(notice_key, notice_message))
        )
        return RedirectResponse(
            f"/admin/settings?language={language}&saved=1&tab=notices", status_code=303
        )

    @app.post("/admin/notices/run")
    def run_due_notices():
        send_due_notices(app.state.store)
        return RedirectResponse("/admin/settings?notice=ran&tab=schedule", status_code=303)

    @app.post("/admin/partners/{partner_id}/archive")
    def archive_partner(partner_id: int):
        if not app.state.store.deactivate_partner(partner_id):
            raise HTTPException(404, "Partner not found")
        return RedirectResponse("/admin/partners", status_code=303)

    @app.post("/admin/partners/{partner_id}/delete")
    def delete_partner(partner_id: int):
        """Backward-compatible alias for old forms and bookmarks."""
        return archive_partner(partner_id)

    @app.post("/admin/partners/{partner_id}/restore")
    def restore_partner(partner_id: int):
        if not app.state.store.restore_partner(partner_id):
            raise HTTPException(404, "Archived partner not found")
        return RedirectResponse("/admin/partners?view=archived", status_code=303)

    @app.post("/admin/partners/{partner_id}/telegram/unlink")
    def unlink_telegram(partner_id: int):
        if not app.state.store.unlink_telegram_chat(partner_id):
            raise HTTPException(404, "Partner not found or already unlinked")
        return RedirectResponse(
            f"/admin/partners/{partner_id}/edit?unlinked=1#contact",
            status_code=303,
        )

    @app.post("/admin/partners/{partner_id}/telegram/invite/rotate")
    def rotate_telegram_invite(partner_id: int, request: Request):
        """Revoke the current invite and mint a new one.

        Used by the partner card UI when the admin wants to invalidate a
        link that has been shared but not yet used (e.g. sent to the wrong
        person). The Telegram handle itself is not touched — use the
        partner-edit flow for that.
        """
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        if not telegram_token():
            raise HTTPException(409, "Telegram bot is not configured")
        if not (partner["telegram"] or "").strip():
            raise HTTPException(409, "Set a Telegram handle first")
        if not partner.get("telegram_chat_id"):
            new_invite = app.state.store.create_telegram_invite(
                partner_id,
                expected_handle=partner["telegram"],
                actor=_session_actor(request),
            )
            params = {
                "tg_rotated": "1",
                "tg_invite_token": new_invite["token"],
            }
        else:
            # Chat already linked — no new invite needed. Surface a flash
            # message so the admin knows nothing was rotated.
            params = {"tg_already_linked": "1"}
        return RedirectResponse(
            f"/admin/partners/{partner_id}/edit?{urlencode(params)}#contact",
            status_code=303,
        )

    @app.get("/reports/{report_id}", response_class=HTMLResponse)
    def report_detail(request: Request, report_id: int):
        report = app.state.store.report(report_id)
        if not report:
            raise HTTPException(404, "Отчёт не найден")
        report["application_breakdown"] = application_breakdown_with_usage(
            report["partner_id"], report["period"], report.get("application_breakdown", [])
        )
        return TEMPLATES.TemplateResponse(
            request,
            "report.html",
            context(request, report=report),
        )

    @app.post("/reports/{report_id}/approve")
    def approve_report(report_id: int):
        report = app.state.store.report(report_id)
        if not report:
            raise HTTPException(404, "Отчёт не найден")
        if report["workflow_status"] == "posted":
            return RedirectResponse(f"/reports/{report_id}", status_code=303)
        app.state.store.approve_report(report_id, approved_by=admin_user)
        return RedirectResponse(f"/reports/{report_id}", status_code=303)

    @app.post("/reports/{report_id}/unapprove")
    def unapprove_report(report_id: int):
        report = app.state.store.report(report_id)
        if not report:
            raise HTTPException(404, "Отчёт не найден")
        if report["workflow_status"] == "posted":
            # Posted reports need a separate unpost step first.
            app.state.store.unpost_report(report_id, actor=admin_user)
        app.state.store.unapprove_report(report_id, actor=admin_user)
        return RedirectResponse(f"/reports/{report_id}", status_code=303)

    @app.post("/reports/{report_id}/unpost")
    def unpost_report(report_id: int):
        report = app.state.store.report(report_id)
        if not report:
            raise HTTPException(404, "Отчёт не найден")
        app.state.store.unpost_report(report_id, actor=admin_user)
        return RedirectResponse(f"/reports/{report_id}", status_code=303)

    @app.get("/export/{period}.csv")
    def export_period(
        period: str,
        status: str = "all",
        risk: str = "all",
        q: str = "",
        sort: str = DEFAULT_SORT,
        dir: str = "asc",
    ):
        filters = dashboard_filters(status, risk, q)
        items = sort_dashboard(
            [
                item
                for item in app.state.store.dashboard(period)
                if matches_dashboard_filters(item, filters)
            ],
            sort if sort in DASHBOARD_SORTS else DEFAULT_SORT,
            "desc" if dir == "desc" else "asc",
        )
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "Partner",
                "Period",
                "Turnover",
                "Currency",
                "Rate",
                "Expected Share",
                "Reported Share",
                "Clients",
                "Risk",
                "Status",
                "Status Updated At",
            ]
        )
        for item in items:
            if item["report_id"] is None:
                continue
            rate = item["rate"]
            expected = round(item["turnover"] * rate, 2) if rate is not None else ""
            writer.writerow(
                [
                    item["name"],
                    period,
                    item["turnover"],
                    item["report_currency"],
                    rate if rate is not None else "",
                    expected,
                    item["reported_share"] if item["reported_share"] is not None else "",
                    item["client_count"],
                    item["risk_level"],
                    item["status"],
                    item.get("status_updated_at") or "",
                ]
            )
        headers = {
            "Content-Disposition": f'attachment; filename="revshare-{period}.csv"'
        }
        return StreamingResponse(
            iter([stream.getvalue().encode("utf-8-sig")]),
            media_type="text/csv",
            headers=headers,
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    uvicorn.run(
        "revshare.web:app",
        host=os.getenv("REVSHARE_HOST", "0.0.0.0"),
        port=43127,
        reload=False,
    )

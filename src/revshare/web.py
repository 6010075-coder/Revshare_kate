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
from urllib.parse import urlencode, urlparse

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from revshare.countries import (
    ALTEGIO_LEGAL_ENTITIES,
    APP_CATEGORIES,
    REPORT_CHANNELS,
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
from revshare.altegio_sync import sync_period
from revshare.application_breakdown import add_usage_checks, breakdown_for_report
from revshare.mysql import mysql_configured
from revshare.dispatch import partner_letter, send_due_notices, send_partner_notice
from revshare.mailer import mail_ready
from revshare.extract import parse_partner_report
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
from revshare.secrets import load_secrets, public_base_url, telegram_token
from revshare.timezone import format_local_datetime
from revshare.sheet_sync import flush_queued_sheet_writes
from revshare import sheets
from revshare.store import (
    CLAIM_REASON_KEYS,
    DEFAULT_DB,
    Store,
    assess,
    discrepancy_items,
    normalize_application_links,
    normalize_ids,
)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_REPORT_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SNAPSHOT_UPLOAD_BYTES = 5 * 1024 * 1024
log = logging.getLogger("revshare.web")


PARTNER_TABS = ("main", "settings", "history")
REVIEW_TABS = ("summary", "differences")


def review_active_tab(
    request: Request,
    *,
    required_count: int,
    form_error: str | None = None,
) -> str:
    tab = request.query_params.get("tab", "")
    if tab in REVIEW_TABS:
        return tab
    if form_error == "explanations_required" or required_count:
        return "differences"
    return "summary"


DASHBOARD_STATUSES = ("all", "missing", "submitted", "approved", "review", "verified")
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
    """Run due notices hourly; send logs make successful deliveries idempotent."""
    while True:
        try:
            await asyncio.to_thread(send_due_notices, store)
        except Exception:
            log.exception("Notice scheduler run failed")
        await asyncio.sleep(3600)


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
        and app.state.store.setting("notice_scheduler_enabled", "1") == "1"
    ):
        tasks.append(asyncio.create_task(_notice_scheduler_loop(app.state.store)))
    yield
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app(
    db_path: Path | None = None, *, admin_auth: bool | None = None
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

    @app.middleware("http")
    async def protect_admin(request: Request, call_next):
        path = request.url.path
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
                    valid = hmac.compare_digest(username, admin_user) and hmac.compare_digest(
                        password, admin_password
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
            form = await request.form()
            supplied_token = str(form.get("csrf_token") or "")
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

    def context(request: Request, **values: object) -> dict:
        partner_page = "language_override" in values
        language = str(values.pop("language_override", "")) or current_language(request)
        selected_period = str(values.get("period") or current_period())
        path = request.url.path
        query = str(request.url.query)
        language_next = path + (f"?{query}" if query else "")
        return {
            "request": request,
            "current_period": current_period(),
            "period_options": recent_periods(selected_period),
            "lang": language,
            "partner_page": partner_page,
            "ui_languages": SUPPORTED_LANGUAGES if partner_page else ADMIN_LANGUAGES,
            "language_labels": LANGUAGE_LABELS,
            "language_next": language_next,
            "t": translator(language),
            "csrf_token": app.state.csrf_token,
            "months_word": lambda count: months_word(language, int(count)),
            "countries": countries_for(language),
            "format_local": format_local_datetime,
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
        sort: str = DEFAULT_SORT,
        dir: str = "asc",
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

    @app.get("/submit/{token}/template.csv")
    def report_template(token: str, period: str | None = None):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        selected = valid_period(period) or current_period()
        stream = io.StringIO()
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(["Application", "Application URL", "Location ID", "Turnover", "Currency"])
        links = app.state.store.application_links(partner["id"])
        for link in links:
            writer.writerow([link.get("comment", ""), link.get("url", ""), "", "", partner["currency"]])
        if not links:
            writer.writerow(["", "", "", "", partner["currency"]])
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
        return RedirectResponse(
            f"/submit/{token}/review/{draft_token}",
            status_code=303,
        )

    @app.get("/submit/{token}/review/{draft_token}", response_class=HTMLResponse)
    def review_report(
        request: Request,
        token: str,
        draft_token: str,
    ):
        partner = app.state.store.partner_by_token(token)
        if not partner:
            raise HTTPException(404, "Ссылка партнёра не найдена")
        draft = app.state.store.report_draft(draft_token, partner["id"])
        if not draft:
            raise HTTPException(404, "Draft not found")
        path = UPLOADS / draft["attachment"]
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
        connected, active = app.state.store.usage(partner["id"], draft["period"])
        check = assess(draft["location_ids"], connected, active)
        discrepancies = discrepancy_items(draft["location_ids"], connected, active)
        application_breakdown = application_breakdown_with_usage(
            partner["id"], draft["period"], draft.get("application_breakdown", [])
        )
        long_idle = long_idle_locations(
            app.state.store, partner["id"], draft["period"], discrepancies
        )
        required = [item for item in discrepancies if item["required"]]
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
                discrepancies=discrepancies,
                required_discrepancies=required,
                optional_discrepancies=[
                    item for item in discrepancies if not item["required"]
                ],
                application_breakdown=application_breakdown,
                long_idle=long_idle,
                idle_months_threshold=app.state.store.idle_months_threshold(),
                form_error=None,
                active_tab=review_active_tab(
                    request, required_count=len(required)
                ),
            ),
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
        connected, active = app.state.store.usage(partner["id"], draft["period"])
        discrepancies = discrepancy_items(ids, connected, active)
        long_idle = long_idle_locations(
            app.state.store, partner["id"], draft["period"], discrepancies
        )
        submitted_claims = {
            (location, kind): {"reason": reason, "detail": detail.strip()}
            for location, kind, reason, detail in zip(
                claim_location, claim_kind, claim_reason, claim_detail
            )
        }
        no_payment_declared = idle_declaration == "no_payment"
        claims = []
        invalid_claims = False
        for item in discrepancies:
            if no_payment_declared and item["kind"] == "connected_not_active":
                claims.append({**item, "reason": "no_payment", "detail": ""})
                continue
            submitted = submitted_claims.get((item["location_id"], item["kind"]))
            item["selected_reason"] = submitted["reason"] if submitted else ""
            item["selected_detail"] = submitted["detail"] if submitted else ""
            required = bool(item["required"])
            if required and (
                not submitted or submitted["reason"] not in CLAIM_REASON_KEYS
            ):
                invalid_claims = True
                continue
            if submitted and submitted["reason"] == "other" and not submitted["detail"]:
                if required:
                    invalid_claims = True
                continue
            if submitted and submitted["reason"] in CLAIM_REASON_KEYS:
                claims.append({**item, **submitted})
        if invalid_claims:
            required = [item for item in discrepancies if item["required"]]
            return TEMPLATES.TemplateResponse(
                request,
                "review.html",
                context(
                    request,
                    language_override=partner_page_language(request, partner),
                    partner=dict(partner),
                    draft=draft,
                    check=assess(ids, connected, active),
                    connected_count=len(connected),
                    active_count=len(active),
                    discrepancies=discrepancies,
                    required_discrepancies=required,
                    optional_discrepancies=[
                        item for item in discrepancies if not item["required"]
                    ],
                    application_breakdown=application_breakdown_with_usage(
                        partner["id"], draft["period"], draft.get("application_breakdown", [])
                    ),
                    long_idle=long_idle,
                    idle_months_threshold=app.state.store.idle_months_threshold(),
                    form_error="explanations_required",
                    comment=comment,
                    idle_declaration=idle_declaration,
                    active_tab=review_active_tab(
                        request,
                        required_count=len(required),
                        form_error="explanations_required",
                    ),
                ),
                status_code=422,
            )
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
            ),
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
    def partners_page(request: Request):
        partners = [dict(row) for row in app.state.store.partners()]
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
            context(request, partners=partners),
        )

    @app.post("/admin/partners")
    def add_partner(
        name: str = Form(...),
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
        if not name.strip():
            raise HTTPException(422, "Partner name is required")
        app.state.store.add_partner(
            name=name.strip(),
            legal_name=legal_name.strip(),
            legal_country="",
            altegio_legal_entity="",
            contact_name="",
            email="",
            telegram="",
            report_channel="email",
            token=token,
            rate=rate_value,
            currency="USD",
        )
        return RedirectResponse("/admin/partners", status_code=303)

    @app.get("/admin/partners/{partner_id}/edit", response_class=HTMLResponse)
    def edit_partner_page(
        request: Request, partner_id: int, tab: str = "", saved: str = ""
    ):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        payload = dict(partner)
        payload["legal_country"] = country_label(
            payload["legal_country"], current_language(request)
        )
        settings = payload["notice_settings"]
        history = app.state.store.partner_history(partner_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partner_edit.html",
            context(
                request,
                partner=payload,
                history=history,
                history_chart=history_chart(history),
                application_links=app.state.store.application_links(partner_id),
                altegio_legal_entities=ALTEGIO_LEGAL_ENTITIES,
                report_channels=REPORT_CHANNELS,
                app_categories=APP_CATEGORIES,
                notice_languages=NOTICE_LANGUAGES,
                notice_settings=settings,
                planned_dates={
                    item["key"]: item["send_on"].isoformat()
                    for item in schedule_for_period(
                        settings,
                        current_period(),
                        name=payload["name"],
                        link=f"/submit/{payload['token']}",
                    )
                },
                bot_username=app.state.store.setting("telegram_bot_username")
                or os.getenv("TELEGRAM_BOT_USERNAME", ""),
                public_base_url=public_base_url(),
                notice_result=request.query_params.get("notice"),
                notice_reason=request.query_params.get("reason"),
                mail_ready=mail_ready(),
                active_tab=tab if tab in PARTNER_TABS else PARTNER_TABS[0],
                partner_tabs=PARTNER_TABS,
                saved=saved == "1",
            ),
        )

    @app.post("/admin/partners/{partner_id}/edit")
    def edit_partner(
        partner_id: int,
        name: str = Form(...),
        legal_name: str = Form(""),
        legal_country: str = Form(""),
        altegio_legal_entity: str = Form(""),
        contact_name: str = Form(""),
        email: str = Form(""),
        telegram: str = Form(""),
        report_channel: str = Form(...),
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
    ):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        altegio_legal_entity = altegio_legal_entity.strip()
        if altegio_legal_entity and altegio_legal_entity not in ALTEGIO_LEGAL_ENTITIES:
            raise HTTPException(422, "Invalid Altegio legal entity")
        if report_channel not in REPORT_CHANNELS:
            raise HTTPException(422, "Invalid report channel")
        if currency.upper() not in SUPPORTED_CURRENCIES:
            raise HTTPException(422, "Unsupported currency")
        try:
            links = normalize_application_links(
                application_url, application_comment, application_category
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not links:
            raise HTTPException(422, "At least one application link is required")
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
        app.state.store.update_partner(
            partner_id=partner_id,
            name=name.strip(),
            legal_name=legal_name.strip(),
            legal_country=canonical_country(legal_country),
            altegio_legal_entity=altegio_legal_entity,
            contact_name=contact_name.strip(),
            email=email.strip(),
            telegram=telegram.strip(),
            report_channel=report_channel,
            rate=rate_value,
            currency=currency.upper(),
            notice_settings=notice_settings,
            category=links[0]["category"] if links else "",
            internal_comment=internal_comment.strip(),
        )
        app.state.store.replace_application_links(partner_id, links)
        return RedirectResponse("/admin/partners", status_code=303)

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

    @app.get("/admin/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request,
        language: str = "",
        saved: str = "",
        period: str = "",
    ):
        selected = language if language in NOTICE_LANGUAGES else current_language(request)
        if selected not in NOTICE_LANGUAGES:
            selected = "en"
        stored_url = app.state.store.setting("public_base_url") or public_base_url()
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
                altegio_mysql_ready=mysql_configured(),
                snapshot_api_ready=bool(snapshot_url()),
                notice_languages=NOTICE_LANGUAGES,
                partner_notice_keys=PARTNER_NOTICE_KEYS,
                notice_keys=NOTICE_KEYS,
                templates=app.state.store.notice_templates(selected),
                fx_currencies=SUPPORTED_CURRENCIES,
                fx_rates=fx["rates"],
                fx_info=fx,
                public_form_url=stored_url,
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
                    "notice_scheduler_enabled", "1"
                )
                == "1",
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

    @app.post("/admin/settings/general")
    def save_general_settings(
        public_form_url: str = Form("", alias="public_base_url"),
        notice_scheduler_enabled: str = Form(""),
    ):
        public_form_url = public_form_url.strip().rstrip("/")
        if public_form_url:
            parsed_url = urlparse(public_form_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise HTTPException(422, "Public URL must be an http(s) URL")
        app.state.store.set_setting("public_base_url", public_form_url)
        app.state.store.set_setting(
            "notice_scheduler_enabled",
            "1" if notice_scheduler_enabled in {"on", "true", "1"} else "0",
        )
        return RedirectResponse("/admin/settings?saved=1#general", status_code=303)

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
        return RedirectResponse("/admin/settings?saved=1#telegram", status_code=303)

    @app.post("/admin/settings/schedule")
    def save_global_schedule(
        notice_on: list[str] = Form(default=[]),
        notice_key: list[str] = Form(default=[]),
        notice_day: list[str] = Form(default=[]),
    ):
        app.state.store.set_global_schedule(
            notice_on, dict(zip(notice_key, notice_day))
        )
        return RedirectResponse("/admin/settings?saved=1#schedule", status_code=303)

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
            f"/admin/settings?language={language}&saved=1#notices", status_code=303
        )

    @app.get("/admin/partners/{partner_id}/letter/{notice_key}", response_class=HTMLResponse)
    def preview_partner_letter(partner_id: int, notice_key: str):
        partner = app.state.store.partner(partner_id)
        if not partner or not partner["active"]:
            raise HTTPException(404, "Partner not found")
        if notice_key not in PARTNER_NOTICE_KEYS:
            raise HTTPException(404, "Notice not found")
        letter = partner_letter(
            partner, current_period(), notice_key, store=app.state.store
        )
        return HTMLResponse(letter["html"])

    @app.post("/admin/notices/run")
    def run_due_notices():
        send_due_notices(app.state.store)
        return RedirectResponse("/admin/settings?notice=ran#telegram", status_code=303)

    @app.post("/admin/partners/{partner_id}/delete")
    def delete_partner(partner_id: int):
        if not app.state.store.deactivate_partner(partner_id):
            raise HTTPException(404, "Partner not found")
        return RedirectResponse("/admin/partners", status_code=303)

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
    uvicorn.run("revshare.web:app", host="0.0.0.0", port=43127, reload=False)

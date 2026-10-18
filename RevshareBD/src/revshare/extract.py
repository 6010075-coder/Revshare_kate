from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader

from revshare.fx import valid_period

MONEY_RE = re.compile(
    r"(?<![\w.])(?P<amount>"
    r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,6})?"
    r"|\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,6})?"
    r"|\d+(?:[.,]\d{1,6})?"
    r")(?![\d\w])"
)
CURRENCY_RE = re.compile(r"\b(USD|EUR|AMD|RUB|KZT|AED|TRY|UAH|GBP|PLN|CZK)\b", re.I)
TOTAL_HINTS = (
    "total",
    "оборот",
    "выручка",
    "gmv",
    "turnover",
    "revenue",
    "revshare",
    "ревшер",
    "к оплате",
    "due",
    "share",
)


@dataclass
class AmountCandidate:
    amount: float
    raw: str
    currency: str | None = None
    label: str = ""
    confidence: str = "low"


@dataclass
class ExtractedReport:
    source: str
    partner_hint: str
    candidates: list[AmountCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        return payload


@dataclass
class ParsedPartnerReport:
    turnover: float | None
    currency: str | None
    reported_share: float | None
    location_ids: list[str]
    location_source: str
    notes: list[str] = field(default_factory=list)
    application_breakdown: list[dict[str, object]] = field(default_factory=list)
    report_reasons: dict[str, str] = field(default_factory=dict)
    report_reason_details: dict[str, str] = field(default_factory=dict)
    all_location_ids: list[str] = field(default_factory=list)
    report_payment_statuses: dict[str, str] = field(default_factory=dict)
    report_status_details: dict[str, str] = field(default_factory=dict)


def _parse_amount(raw: str) -> float | None:
    cleaned = raw.strip().replace("\u00a0", " ").replace(" ", "")
    if cleaned.count(",") == 1 and cleaned.count(".") == 0:
        cleaned = cleaned.replace(",", ".")
    elif cleaned.count(".") > 1 and "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(",") > 1:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


TURNOVER_HINTS = (
    "turnover",
    "revenue",
    "gmv",
    "amount",
    "оборот",
    "выручка",
    "виручка",
    "сумма",
)
SHARE_HINTS = (
    "revshare",
    "revenue share",
    "reward",
    "commission",
    "комиссия",
    "комісія",
    "altegio",
    "к оплате",
    "до сплати",
)
CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "₸": "KZT",
    "₴": "UAH",
    "₽": "RUB",
}


def _currency_from_text(text: str, default: str | None) -> str | None:
    code = CURRENCY_RE.search(text)
    if code:
        return code.group(1).upper()
    for symbol, currency in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return currency
    return default


def _value_from_cell(cell: str) -> float | None:
    if not cell.strip() or "%" in cell:
        return None
    match = MONEY_RE.search(cell)
    return _parse_amount(match.group("amount")) if match else None


def _signed_value_from_cell(cell: str) -> float | None:
    value = _value_from_cell(cell)
    if value is None:
        return None
    if cell.strip().startswith(("-", "−")):
        return -abs(value)
    return value


def _total_line_values(rows: list[list[str]]) -> tuple[float | None, float | None]:
    """Read explicit total rows, keeping turnover and Altegio share apart."""
    turnover = None
    share = None
    for row in rows:
        line = " ".join(cell for cell in row if cell.strip())
        if not _is_total_line(line):
            continue
        match = None
        for match in MONEY_RE.finditer(line):
            pass
        if match is None:
            continue
        value = _parse_amount(match.group("amount"))
        if value is None:
            continue
        folded = line.casefold()
        if any(hint in folded for hint in SHARE_HINTS):
            share = value
        else:
            turnover = value
    return turnover, share


def _labeled_value(rows: list[list[str]], hints: tuple[str, ...]) -> float | None:
    matches: list[float] = []
    for row in rows:
        for column, cell in enumerate(row):
            folded = cell.casefold()
            if not any(hint in folded for hint in hints):
                continue
            if _is_location_header(cell):
                continue
            own_value = _value_from_cell(cell)
            if own_value is not None:
                matches.append(own_value)
            for adjacent in row[column + 1 : column + 3]:
                value = _value_from_cell(adjacent)
                if value is not None:
                    matches.append(value)
                    break
    return matches[-1] if matches else None


def _column_total(rows: list[list[str]], hints: tuple[str, ...]) -> float | None:
    for header_index, row in enumerate(rows[:30]):
        for column, cell in enumerate(row):
            folded = cell.casefold().strip()
            if not any(hint in folded for hint in hints):
                continue
            if _is_location_header(cell):
                continue
            values: list[float] = []
            explicit_total = None
            for data_row in rows[header_index + 1 :]:
                if column >= len(data_row):
                    continue
                value = _value_from_cell(data_row[column])
                if value is None:
                    continue
                row_label = " ".join(data_row[:column]).casefold()
                if any(label in row_label for label in ("total", "итого", "всего", "разом")):
                    explicit_total = value
                else:
                    values.append(value)
            if explicit_total is not None:
                return explicit_total
            if values:
                return round(sum(values), 2)
    return None


def _standard_partner_workbook(
    path: Path,
    period: str | None = None,
    default_currency: str | None = None,
) -> ParsedPartnerReport | None:
    """Read the recommended Integration Hub partner report.

    Partners fill the input columns; formula cells may have no cached values,
    so totals are calculated here from amount, FX and contractual rate.

    ``default_currency`` is the partner's contractual report currency. It is
    used as the fallback when no currency hint is found in the header row
    (the new 9-column template states it in the header block, not in a
    parenthetical inside a column header).
    """
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return None
    workbook = load_workbook(path, data_only=True, read_only=True)
    wanted = period_sheet_titles(period)

    def _sheet_title_tokens(sheet) -> set[str]:
        # Use both the full normalised title and its first token so
        # period-month matches like "Report (EN) — September 2026" still
        # hit the wanted set ("september 2026") via substring logic.
        return {token for token in " ".join(sheet.title.split()).casefold().split(" — ")}

    def _is_skip(sheet) -> bool:
        return any(
            token in " ".join(sheet.title.split()).casefold()
            for token in SHEET_SKIP_TOKENS
        )

    def _matches_period(sheet) -> bool:
        if not wanted:
            return True
        title_norm = " ".join(sheet.title.split()).casefold()
        # Substring match against any period title token ("september 2026",
        # "2026-09", etc.) — handles titles like "Report (EN) — September
        # 2026" that embed the month instead of being exactly it.
        return any(want in title_norm for want in wanted)

    sheets = [
        sheet
        for sheet in workbook.worksheets
        if _matches_period(sheet) and not _is_skip(sheet)
    ]
    if not sheets:
        # Period filter matched nothing — fall back to any sheet that
        # doesn't look like a sample. This preserves the old behaviour
        # for partner-submitted files whose sheet title may not embed
        # the reporting month.
        sheets = [sheet for sheet in workbook.worksheets if not _is_skip(sheet)]
    for sheet in sheets:
        rows = _sheet_rows(sheet)
        header_index = None
        columns: dict[str, int] = {}
        for index, row in enumerate(rows[:20]):
            for column, cell in enumerate(row):
                folded = " ".join(cell.split()).casefold()
                if _is_location_header(cell):
                    columns["location"] = column
                if _is_application_id_header(cell):
                    columns["application_id"] = column
                elif _is_application_header(cell):
                    columns["application"] = column
                if _is_client_header(cell) and "client" not in columns:
                    columns["client"] = column
                if (
                    _is_subscription_period_header(cell)
                    and "subscription_period" not in columns
                ):
                    columns["subscription_period"] = column
                if (
                    _is_settlement_currency_header(cell)
                    and "settlement_currency" not in columns
                ):
                    columns["settlement_currency"] = column
                if (
                    "received by partner" in folded
                    or "получено партнёром" in folded
                ) and "report currency" not in folded and "валюта отчёта" not in folded:
                    columns["amount"] = column
                if folded.startswith("payment currency") or folded.startswith(
                    "валюта платежа"
                ):
                    columns["currency"] = column
                if "monthly avg rate" in folded or "среднемес. курс" in folded:
                    columns["fx"] = column
                if (
                    ("altegio" in folded and "rate" in folded)
                    or ("ставка" in folded and "altegio" in folded)
                ):
                    columns["rate"] = column
            if {"location", "amount", "currency"} <= columns.keys():
                header_index = index
                break
        if header_index is None:
            continue

        location_ids: list[str] = []
        turnover = 0.0
        share = 0.0
        used_rows = 0
        skipped_fx = 0
        report_currency = (
            (default_currency or "").upper() if default_currency else "USD"
        )
        application_groups: dict[str, dict[str, object]] = {}
        header_text = " ".join(rows[header_index])
        report_match = re.search(
            r"\((USD|EUR|AMD|RUB|KZT|AED|TRY|UAH|GBP|PLN|CZK)"
            r"(?:,\s*(?:report currency|валюта отч[её]та))?\)",
            header_text,
            re.I,
        )
        if report_match:
            report_currency = report_match.group(1).upper()

        for row in rows[header_index + 1 :]:
            line = " ".join(row)
            if _is_total_line(line):
                break
            location_cell = (
                _plain_id_cell(row[columns["location"]])
                if columns["location"] < len(row)
                else ""
            )
            location_match = LOCATION_COLUMN_ID_RE.search(location_cell)
            if not location_match:
                continue
            current_location = location_match.group(1)
            location_ids.append(current_location)
            application_ref = ""
            for application_column in ("application_id", "application"):
                if application_column in columns and columns[application_column] < len(row):
                    application_ref = row[columns[application_column]].strip()
                    if application_ref:
                        break
            client_name = ""
            if "client" in columns and columns["client"] < len(row):
                client_name = row[columns["client"]].strip()
            subscription_period = ""
            if (
                "subscription_period" in columns
                and columns["subscription_period"] < len(row)
            ):
                subscription_period = row[columns["subscription_period"]].strip()
            settlement_currency = ""
            if (
                "settlement_currency" in columns
                and columns["settlement_currency"] < len(row)
            ):
                settlement_currency = row[columns["settlement_currency"]].strip()
            group_key = application_ref or "__unassigned__"
            group = application_groups.setdefault(
                group_key,
                {
                    "application_ref": application_ref,
                    "turnover": 0.0,
                    "reported_share": 0.0,
                    "currency": report_currency,
                    "location_ids": [],
                    "line_items": [],
                },
            )
            group["location_ids"].append(current_location)
            amount_cell = row[columns["amount"]] if columns["amount"] < len(row) else ""
            amount = _signed_value_from_cell(amount_cell)
            if amount is None:
                continue
            currency_cell = (
                row[columns["currency"]] if columns["currency"] < len(row) else ""
            )
            payment_currency = _currency_from_text(currency_cell, report_currency)
            fx = 1.0
            if payment_currency != report_currency:
                fx_column = columns.get("fx")
                fx_cell = (
                    row[fx_column]
                    if fx_column is not None and fx_column < len(row)
                    else ""
                )
                fx = _value_from_cell(fx_cell) or 0.0
                if not fx:
                    skipped_fx += 1
                    continue
            converted = amount * fx
            turnover += converted
            rate = None
            if "rate" in columns and columns["rate"] < len(row):
                rate = _value_from_cell(row[columns["rate"]])
            row_share: float | None = None
            if rate is not None:
                share += converted * rate
                row_share = round(converted * rate, 2)
            group["turnover"] = round(float(group["turnover"]) + converted, 2)
            if row_share is not None:
                group["reported_share"] = round(
                    float(group["reported_share"]) + row_share, 2
                )
            line_items = group["line_items"]
            assert isinstance(line_items, list)
            line_items.append(
                {
                    "location_id": current_location,
                    "client_name": client_name,
                    "subscription_period": subscription_period,
                    "payment_currency": payment_currency,
                    "amount_in_payment_currency": round(amount, 2),
                    "fx_rate": round(fx, 6),
                    "settlement_currency": settlement_currency or None,
                    "amount_in_report_currency": round(converted, 2),
                    "altegio_rate": rate,
                    "due_to_altegio": row_share,
                }
            )
            used_rows += 1

        if not location_ids:
            continue
        notes = ["recommended_template"]
        if skipped_fx:
            notes.append("rows_missing_fx")
        if not used_rows:
            notes.append("turnover_not_found")
        application_breakdown = []
        if ("application" in columns or "application_id" in columns) and application_groups:
            notes.append("application_column")
            if "__unassigned__" in application_groups:
                notes.append("application_not_identified")
            for group in application_groups.values():
                application_breakdown.append(
                    {
                        **group,
                        "location_ids": _unique(group["location_ids"]),
                    }
                )
        else:
            # No application grouping column found — still surface per-row
            # data so the partner's per-customer line items survive the
            # round-trip even when only location IDs are available.
            application_breakdown = [
                {
                    "application_ref": "",
                    "turnover": round(turnover, 2),
                    "reported_share": round(share, 2) if share else None,
                    "currency": report_currency,
                    "location_ids": _unique(location_ids),
                    "line_items": [
                        item
                        for group in application_groups.values()
                        for item in group["line_items"]
                    ],
                }
            ]
        return ParsedPartnerReport(
            turnover=round(turnover, 2) if used_rows else None,
            currency=report_currency,
            reported_share=round(share, 2) if share else None,
            location_ids=_unique(location_ids),
            location_source="recommended_template",
            notes=notes,
            application_breakdown=application_breakdown,
        )
    return None


def _structured_application_report(
    path: Path,
    rate: float | None,
    default_currency: str | None,
    period: str | None,
) -> ParsedPartnerReport | None:
    """Read a simple one-row-per-application/location CSV or workbook."""
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".xls", ".csv"}:
        return None
    try:
        rows = _rows_from_file(path, period)
    except Exception:
        return None
    columns: dict[str, int] = {}
    header_index = None
    for index, row in enumerate(rows[:20]):
        for column, cell in enumerate(row):
            folded = " ".join(cell.split()).casefold()
            if _is_application_id_header(cell):
                columns["application_id"] = column
            elif _is_application_header(cell):
                columns["application"] = column
            if _is_location_header(cell):
                columns["location"] = column
            if any(hint in folded for hint in TURNOVER_HINTS) and not any(
                hint in folded for hint in SHARE_HINTS
            ):
                columns["amount"] = column
            if "currency" in folded or "валют" in folded:
                columns["currency"] = column
            if any(hint in folded for hint in SHARE_HINTS):
                columns["share"] = column
        if {"location", "amount"} <= columns.keys() and (
            "application" in columns or "application_id" in columns
        ):
            header_index = index
            break
    if header_index is None:
        return None

    groups: dict[str, dict[str, object]] = {}
    location_ids: list[str] = []
    currencies: set[str] = set()
    total_share = 0.0
    turnover = 0.0
    for row in rows[header_index + 1 :]:
        if _is_total_line(" ".join(row)):
            continue
        if columns["location"] >= len(row):
            continue
        location_match = LOCATION_COLUMN_ID_RE.search(
            _plain_id_cell(row[columns["location"]])
        )
        if not location_match:
            continue
        location_id = location_match.group(1)
        application_ref = ""
        for application_column in ("application_id", "application"):
            if application_column in columns and columns[application_column] < len(row):
                application_ref = row[columns[application_column]].strip()
                if application_ref:
                    break
        group_key = application_ref or "__unassigned__"
        currency = default_currency or "USD"
        if "currency" in columns and columns["currency"] < len(row):
            currency = _currency_from_text(row[columns["currency"]], currency) or currency
        amount = _signed_value_from_cell(
            row[columns["amount"]] if columns["amount"] < len(row) else ""
        )
        group = groups.setdefault(
            group_key,
            {
                "application_ref": application_ref,
                "turnover": 0.0,
                "reported_share": 0.0,
                "currency": currency,
                "location_ids": [],
            },
        )
        group["location_ids"].append(location_id)
        location_ids.append(location_id)
        if amount is None:
            continue
        currencies.add(currency)
        group["turnover"] = round(float(group["turnover"]) + amount, 2)
        turnover += amount
        share = None
        if "share" in columns and columns["share"] < len(row):
            share = _signed_value_from_cell(row[columns["share"]])
        if share is None and rate is not None:
            share = amount * rate
        if share is not None:
            group["reported_share"] = round(float(group["reported_share"]) + share, 2)
            total_share += share

    if not groups:
        return None
    notes = ["application_column"]
    if "__unassigned__" in groups:
        notes.append("application_not_identified")
    if len(currencies) > 1:
        notes.append("multiple_currencies")
    report_currency = next(iter(currencies), default_currency)
    return ParsedPartnerReport(
        turnover=round(turnover, 2),
        currency=report_currency,
        reported_share=round(total_share, 2) if total_share else None,
        location_ids=_unique(location_ids),
        location_source="application_table",
        notes=notes,
        application_breakdown=[
            {**group, "location_ids": _unique(group["location_ids"])}
            for group in groups.values()
        ],
    )


REPORT_REASON_KEYS = {
    "no_payment",
    "free_trial",
    "pay_next_month",
    "disconnected_before_month",
    "connected_after_month",
    "file_error",
    "other",
}


def _report_column(rows: list[list[str]], names: tuple[str, ...]) -> tuple[int, int] | None:
    for row_index, row in enumerate(rows[:20]):
        for column, cell in enumerate(row):
            folded = " ".join(cell.split()).casefold().strip(" :#")
            if any(name in folded for name in names):
                return row_index, column
    return None


def _normalise_payment_status(raw: str) -> tuple[str, str, str]:
    value = " ".join((raw or "").split()).strip()
    folded = value.casefold()
    if not value:
        return "", "", ""
    aliases = {
        "paid": "paid",
        "оплата получена": "paid",
        "есть оплата": "paid",
        "payment received": "paid",
        "no_payment": "no_payment",
        "нет оплаты": "no_payment",
        "оплаты нет": "no_payment",
        "не платит за приложение": "no_payment",
        "none pay for the app": "no_payment",
        "trial": "trial",
        "триал": "trial",
        "тестовый период": "trial",
        "free_trial": "free_trial",
        "бесплатный период": "free_trial",
        "бесплатный месяц": "free_trial",
        "free period": "free_trial",
        "free_month": "free_trial",
        "pay_next_month": "pay_next_month",
        "оплата в следующем месяце": "pay_next_month",
        "платит в следующем месяце": "pay_next_month",
        "disconnected_before_month": "disconnected_before_month",
        "салон закрыт": "disconnected_before_month",
        "салон отключён": "disconnected_before_month",
        "салон отключен": "disconnected_before_month",
        "connected_after_month": "connected_after_month",
        "подключён после месяца": "connected_after_month",
        "подключен после месяца": "connected_after_month",
        "file_error": "file_error",
        "ошибка в отчёте": "file_error",
        "ошибка в отчете": "file_error",
        "ошибка в файле": "file_error",
        "other": "other",
        "другое": "other",
    }
    if folded in aliases:
        status = aliases[folded]
        reason = {
            "trial": "free_trial",
            "free_trial": "free_trial",
            "pay_next_month": "pay_next_month",
            "disconnected_before_month": "disconnected_before_month",
            "connected_after_month": "connected_after_month",
            "file_error": "file_error",
            "other": "other",
            "no_payment": "no_payment",
        }.get(status, "")
        return status, reason, ""
    if folded.startswith("другое:") or folded.startswith("other:"):
        return "other", "other", value.split(":", 1)[1].strip()
    # A free-text value is treated as “Other” and retained as its detail.
    return "other", "other", value


def _normalise_report_reason(raw: str) -> tuple[str, str]:
    """Backward-compatible reason parser for older files with a Reason column."""
    _status, reason, detail = _normalise_payment_status(raw)
    return reason, detail


def _report_annotations(
    path: Path, period: str | None
) -> tuple[
    list[str],
    set[str] | None,
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    bool,
]:
    """Read participation and explanation columns from a completed full report."""
    try:
        rows = _rows_from_file(path, period)
    except Exception:
        return [], None, {}, {}, {}, {}, False
    location_header = _report_column(rows, ("location id", "location_id", "id филиала", "altegio id"))
    reason_header = _report_column(rows, ("reason", "причина", "почему"))
    status_header = _report_column(rows, ("payment status", "статус оплаты", "статус платежа", "status"))
    comment_header = _report_column(rows, ("comment", "комментарий", "details", "подробности"))
    participation_header = _report_column(
        rows,
        ("included in calculation", "included", "participates", "участвует", "в расчёте", "в расчете"),
    )
    if not location_header:
        return [], None, {}, {}, {}, {}, False
    header_index, location_column = location_header
    participation_column = (
        participation_header[1]
        if participation_header and participation_header[0] == header_index
        else None
    )
    reason_column = reason_header[1] if reason_header and reason_header[0] == header_index else None
    status_column = status_header[1] if status_header and status_header[0] == header_index else None
    comment_column = comment_header[1] if comment_header and comment_header[0] == header_index else None
    all_ids: list[str] = []
    included: set[str] = set()
    reasons: dict[str, str] = {}
    details: dict[str, str] = {}
    statuses: dict[str, str] = {}
    status_details: dict[str, str] = {}
    for row in rows[header_index + 1 :]:
        if location_column >= len(row):
            continue
        match = LOCATION_COLUMN_ID_RE.search(_plain_id_cell(row[location_column]))
        if not match or _is_total_line(" ".join(row)):
            continue
        location_id = match.group(1)
        all_ids.append(location_id)
        participation = (
            row[participation_column].strip().casefold()
            if participation_column is not None and participation_column < len(row)
            else ""
        )
        if participation in {"да", "yes", "y", "1", "true", "включить", "участвует"}:
            included.add(location_id)
        raw_status = row[status_column] if status_column is not None and status_column < len(row) else ""
        if not raw_status and reason_column is not None and reason_column < len(row):
            raw_status = row[reason_column]
        status, reason, detail = _normalise_payment_status(raw_status)
        if status:
            statuses[location_id] = status
        raw_comment = row[comment_column] if comment_column is not None and comment_column < len(row) else ""
        if raw_comment.strip():
            status_details[location_id] = raw_comment.strip()
        elif detail:
            status_details[location_id] = detail
        if reason:
            reasons[location_id] = reason
            if detail:
                details[location_id] = detail
    return (
        _unique(all_ids),
        included if participation_column is not None else None,
        reasons,
        details,
        statuses,
        status_details,
        status_column is not None,
    )


def _apply_report_annotations(
    parsed: ParsedPartnerReport, path: Path, period: str | None
) -> ParsedPartnerReport:
    all_ids, included, reasons, details, statuses, status_details, has_status_column = _report_annotations(
        path, period
    )
    if included is not None:
        parsed.location_ids = [location_id for location_id in parsed.location_ids if location_id in included]
        # Rows without an amount are still valid rows in the full report.
        if not parsed.location_ids:
            parsed.location_ids = sorted(included)
        parsed.notes.append("participation_column")
    elif has_status_column:
        # In the compact partner format every row is an application/location pair.
        # Rows with a real payment are the rows included in the payout calculation;
        # rows with another status remain in the full register for reconciliation.
        parsed.location_ids = [
            location_id for location_id in all_ids if statuses.get(location_id) == "paid"
        ]
    if has_status_column:
        parsed.notes.append("payment_status_column")
    parsed.all_location_ids = all_ids or list(parsed.location_ids)
    parsed.report_reasons = reasons
    parsed.report_reason_details = details
    parsed.report_payment_statuses = statuses
    parsed.report_status_details = status_details
    return parsed


def parse_partner_report(
    path: Path,
    rate: float | None = None,
    default_currency: str | None = None,
    period: str | None = None,
) -> ParsedPartnerReport:
    standard = _standard_partner_workbook(path, period, default_currency=default_currency)
    if standard is not None:
        if standard.reported_share is None and standard.turnover is not None and rate is not None:
            standard.reported_share = round(standard.turnover * rate, 2)
            standard.notes.append("share_calculated")
        return _apply_report_annotations(standard, path, period)
    structured = _structured_application_report(
        path, rate=rate, default_currency=default_currency, period=period
    )
    if structured is not None:
        return _apply_report_annotations(structured, path, period)
    rows = _rows_from_file(path, period)
    text = "\n".join(" ".join(row) for row in rows)
    location_ids, location_source = location_ids_from_file(path, period)
    notes: list[str] = []

    turnover, reported_share = _total_line_values(rows)
    if turnover is None:
        turnover = _labeled_value(rows, TURNOVER_HINTS) or _column_total(
            rows, TURNOVER_HINTS
        )
    if reported_share is None:
        reported_share = _labeled_value(rows, SHARE_HINTS) or _column_total(
            rows, SHARE_HINTS
        )
    if turnover is None:
        notes.append("turnover_not_found")
    if reported_share is None and turnover is not None and rate is not None:
        reported_share = round(turnover * rate, 2)
        notes.append("share_calculated")
    if not location_ids:
        notes.append("locations_not_found")
    currency = _currency_from_text(text, default_currency)
    parsed = ParsedPartnerReport(
        turnover=turnover,
        currency=currency,
        reported_share=reported_share,
        location_ids=location_ids,
        location_source=location_source,
        notes=notes,
    )
    return _apply_report_annotations(parsed, path, period)


def amounts_from_text(text: str, default_currency: str | None = None) -> list[AmountCandidate]:
    found: list[AmountCandidate] = []
    currency_global = None
    currency_match = CURRENCY_RE.search(text or "")
    if currency_match:
        currency_global = currency_match.group(1).upper()
    for match in MONEY_RE.finditer(text or ""):
        raw = match.group("amount")
        value = _parse_amount(raw)
        if value is None:
            continue
        start = max(0, match.start() - 40)
        label = " ".join((text or "")[start : match.start()].split())
        confidence = "medium" if any(h in label.casefold() for h in TOTAL_HINTS) else "low"
        if value < 1:
            continue
        found.append(
            AmountCandidate(
                amount=value,
                raw=raw,
                currency=(currency_global or default_currency),
                label=label,
                confidence=confidence,
            )
        )
    hinted = [c for c in found if c.confidence == "medium"]
    return hinted or found


def extract_plain(path: Path) -> ExtractedReport:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return ExtractedReport(
        source=str(path),
        partner_hint=path.stem,
        candidates=amounts_from_text(text),
    )


def extract_csv(path: Path) -> ExtractedReport:
    rows: list[str] = []
    with path.open(newline="", encoding="utf-8", errors="ignore") as handle:
        for row in csv.reader(handle):
            rows.append(" ".join(row))
    text = "\n".join(rows)
    return ExtractedReport(
        source=str(path),
        partner_hint=path.stem,
        candidates=amounts_from_text(text),
    )


def extract_xlsx(path: Path) -> ExtractedReport:
    workbook = load_workbook(path, data_only=True, read_only=True)
    notes: list[str] = []
    candidates: list[AmountCandidate] = []
    partner_hint = path.stem
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if cell is None else str(cell) for cell in row]
            line = " ".join(cells)
            if not line.strip():
                continue
            for candidate in amounts_from_text(line):
                candidate.label = f"{sheet.title}: {candidate.label or line[:80]}"
                candidates.append(candidate)
    hinted = [c for c in candidates if c.confidence == "medium"]
    picked = hinted or candidates
    if len(picked) > 8:
        notes.append(f"{len(picked)} numeric candidates; showing hinted totals first")
    return ExtractedReport(
        source=str(path),
        partner_hint=partner_hint,
        candidates=picked[:12],
        notes=notes,
    )


def extract_xls(path: Path) -> ExtractedReport:
    rows = _rows_from_file(path)
    text = "\n".join(" ".join(row) for row in rows)
    return ExtractedReport(
        source=str(path),
        partner_hint=path.stem,
        candidates=amounts_from_text(text),
        notes=["legacy_xls"],
    )


def extract_pdf(path: Path) -> ExtractedReport:
    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    notes = []
    if not text.strip():
        notes.append("PDF has no extractable text; request Excel or a screenshot from the partner")
    return ExtractedReport(
        source=str(path),
        partner_hint=path.stem,
        candidates=amounts_from_text(text),
        notes=notes,
        error="empty pdf text" if not text.strip() else None,
    )


EXTRACTORS = {
    ".txt": extract_plain,
    ".md": extract_plain,
    ".csv": extract_csv,
    ".xlsx": extract_xlsx,
    ".xlsm": extract_xlsx,
    ".xls": extract_xls,
    ".pdf": extract_pdf,
}


def extract_file(path: Path) -> ExtractedReport:
    suffix = path.suffix.lower()
    extractor = EXTRACTORS.get(suffix)
    if extractor is None:
        return ExtractedReport(
            source=str(path),
            partner_hint=path.stem,
            error=f"unsupported type {suffix}",
            notes=["Forward as-is for manual review; screenshots are read in chat, not by this CLI"],
        )
    try:
        return extractor(path)
    except Exception as exc:  # noqa: BLE001 — intake must not crash the month run
        return ExtractedReport(
            source=str(path),
            partner_hint=path.stem,
            error=str(exc),
        )


LOCATION_HEADER_NAMES = {
    "id",
    "ids",
    "location",
    "locations",
    "location id",
    "location_id",
    "salon",
    "salon id",
    "salon_id",
    "branch",
    "branch id",
    "id altegio",
    "altegio id",
    "филиал",
    "id филиала",
    "локация",
    "точка",
    "заклад",
}
LOCATION_HEADER_PHRASES = (
    "location id",
    "location_id",
    "salon id",
    "salon_id",
    "id altegio",
    "altegio id",
    "id филиала",
)
APPLICATION_HEADER_NAMES = {
    "app",
    "application",
    "application url",
    "integration",
    "приложение",
    "интеграция",
}
APPLICATION_HEADER_PHRASES = (
    "application url",
    "приложение",
    "интеграц",
)
APPLICATION_ID_HEADER_NAMES = {
    "app id",
    "application id",
    "приложение id",
    "id приложения",
}
APPLICATION_ID_HEADER_PHRASES = (
    "application id",
    "app id",
    "приложение id",
    "id приложения",
)
TOTAL_LABELS = ("total", "totals", "итого", "всего", "разом")
LOCATION_ID_RE = re.compile(r"(?<![\d.,])(\d{4,8})(?:-alt)?(?![\d.,])", re.I)
LOCATION_COLUMN_ID_RE = re.compile(r"(?<![\d.,])(\d{2,12})(?:-alt)?(?![\d.,])", re.I)
FLOAT_ID_RE = re.compile(r"^\s*(\d+)[.,]0+\s*$")

# Tokens that mark a sheet as a non-data reference (sample/example).
# The new 9-column template ships a "Пример отчёта" / "Sample report"
# companion sheet — the parser must skip it so its example rows never
# leak into reconciliation.
SHEET_SKIP_TOKENS: tuple[str, ...] = (
    "пример",
    "sample",
    "example",
)

CLIENT_HEADER_PHRASES = (
    "client name",
    "client email",
    "client name or client email",
    "client_name",
    "название клиента",
    "имя клиента",
    "email клиента",
)

SUBSCRIPTION_PERIOD_HEADER_PHRASES = (
    "subscription period",
    "paid subscription period",
    "paid subscription",
    "subscription_period",
    "период оплаченной",
    "период оплаты",
    "период подписки",
    "період оплаченої",
    "період підписки",
    "okres subskrypcji",
    "abonnement",
)


def _is_client_header(cell: str) -> bool:
    folded = " ".join(cell.split()).casefold().strip(" :#")
    if not folded or len(folded) > 80:
        return False
    return any(phrase in folded for phrase in CLIENT_HEADER_PHRASES)


def _is_subscription_period_header(cell: str) -> bool:
    folded = " ".join(cell.split()).casefold().strip(" :#")
    if not folded or len(folded) > 80:
        return False
    return any(phrase in folded for phrase in SUBSCRIPTION_PERIOD_HEADER_PHRASES)


SETTLEMENT_CURRENCY_HEADER_PHRASES = (
    "settlement currency",
    "settlement_currency",
    "валюта расчёта",
    "валюта расчета",
    "валюта розрахунку",
    "waluta rozliczenia",
)


def _is_settlement_currency_header(cell: str) -> bool:
    folded = " ".join(cell.split()).casefold().strip(" :#")
    if not folded or len(folded) > 80:
        return False
    return any(phrase in folded for phrase in SETTLEMENT_CURRENCY_HEADER_PHRASES)


def _plain_id_cell(cell: str) -> str:
    """Spreadsheets often store Location IDs as floats, so 1295757.0 is one id."""
    match = FLOAT_ID_RE.match(cell or "")
    return match.group(1) if match else (cell or "")


def _is_location_header(cell: str) -> bool:
    folded = " ".join(cell.split()).casefold().strip(" :#")
    if not folded or len(folded) > 40:
        return False
    if folded in LOCATION_HEADER_NAMES:
        return True
    return any(phrase in folded for phrase in LOCATION_HEADER_PHRASES)


def _is_application_header(cell: str) -> bool:
    folded = " ".join(cell.split()).casefold().strip(" :#")
    if not folded or len(folded) > 60:
        return False
    if folded in APPLICATION_HEADER_NAMES:
        return True
    return any(phrase in folded for phrase in APPLICATION_HEADER_PHRASES)


def _is_application_id_header(cell: str) -> bool:
    folded = " ".join(cell.split()).casefold().strip(" :#")
    if not folded or len(folded) > 60:
        return False
    if folded in APPLICATION_ID_HEADER_NAMES:
        return True
    return any(phrase in folded for phrase in APPLICATION_ID_HEADER_PHRASES)


def _is_total_line(text: str) -> bool:
    folded = text.casefold()
    return any(label in folded for label in TOTAL_LABELS)


def _is_tabular(rows: list[list[str]]) -> bool:
    return any(len([cell for cell in row if cell.strip()]) > 1 for row in rows)


def period_sheet_titles(period: str | None) -> set[str]:
    """Titles that can name the reporting month, so one month is read at a time."""
    checked = valid_period(period or "")
    if not checked:
        return set()
    year, month = checked.split("-")
    titles = {
        checked,
        f"{month}.{year}",
        f"{month}/{year}",
        f"{int(month)}.{year}",
        f"{year}/{month}",
    }
    from revshare.i18n import TRANSLATIONS

    for language in TRANSLATIONS.values():
        name = language.get(f"month_{month}")
        if name:
            titles.add(f"{name} {year}")
            titles.add(f"{name[:3]} {year}")
    return {title.casefold() for title in titles}


def _sheet_rows(sheet) -> list[list[str]]:
    return [
        ["" if cell is None else str(cell) for cell in row]
        for row in sheet.iter_rows(values_only=True)
    ]


def _rows_from_file(path: Path, period: str | None = None) -> list[list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".xls":
        import xlrd

        workbook = xlrd.open_workbook(path, on_demand=True)
        return [
            ["" if cell.value is None else str(cell.value) for cell in row]
            for sheet in workbook.sheets()
            for row in sheet.get_rows()
        ]
    if suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(path, data_only=True, read_only=True)
        wanted = period_sheet_titles(period)

        def _title_norm(sheet) -> str:
            return " ".join(sheet.title.split()).casefold()

        def _is_skip(sheet) -> bool:
            return any(token in _title_norm(sheet) for token in SHEET_SKIP_TOKENS)

        def _matches_period(sheet) -> bool:
            if not wanted:
                return True
            return any(want in _title_norm(sheet) for want in wanted)

        # Prefer sheets that explicitly embed the reporting month.
        if wanted:
            for sheet in workbook.worksheets:
                if _matches_period(sheet) and not _is_skip(sheet):
                    return _sheet_rows(sheet)
        # Fall back to any non-sample sheet (e.g. partner uploads that
        # don't put the month in the sheet title).
        rows: list[list[str]] = []
        for sheet in workbook.worksheets:
            if _is_skip(sheet):
                continue
            rows.extend(_sheet_rows(sheet))
        return rows
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8", errors="ignore") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            return [list(row) for row in csv.reader(handle, dialect)]
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return [[line] for line in text.splitlines()]
    return [[line] for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()]


def location_ids_from_file(
    path: Path, period: str | None = None
) -> tuple[list[str], str]:
    """Read the client registry out of a partner file.

    Returns the ids and how they were found, so the submission can tell the
    partner whether their own file was understood.
    """
    try:
        rows = _rows_from_file(path, period)
    except Exception:  # noqa: BLE001 — a broken upload must not lose the report
        return [], "unreadable"

    if _is_tabular(rows):
        for index, row in enumerate(rows[:10]):
            for column, cell in enumerate(row):
                if not _is_location_header(cell):
                    continue
                found: list[str] = []
                for data_row in rows[index + 1 :]:
                    if column >= len(data_row):
                        continue
                    if _is_total_line(" ".join(data_row)):
                        continue
                    match = LOCATION_COLUMN_ID_RE.search(
                        _plain_id_cell(data_row[column])
                    )
                    if match:
                        found.append(match.group(1))
                if found:
                    return _unique(found), "column"

    scanned: list[str] = []
    for row in rows:
        line = " ".join(row)
        if _is_total_line(line):
            continue
        match = LOCATION_ID_RE.search(line)
        if match:
            scanned.append(match.group(1))
    return (_unique(scanned), "scan") if scanned else ([], "not_found")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


# --- Manual reconciliation base ------------------------------------------------
#
# An admin uploads an Excel/CSV file per month that lists every Location ID the
# partner app saw, with turnover, currency, and an Active flag. The parser is
# intentionally tolerant: bad rows are skipped and recorded in ``notes`` so a
# partially-bad upload never blocks the month-end assess.

_BASE_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "application_id": ("application id", "application", "app id", "app_id"),
    "location_id": ("location id", "location", "location_id"),
    "turnover": ("turnover", "revenue", "gmv", "оборот", "выручка"),
    "currency": ("currency", "валюта"),
    "active": ("active", "is active", "активен"),
}

_TRUE_VALUES = {"1", "true", "yes", "y", "да", "так", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "нет", "ні", "off", ""}


@dataclass
class BaseRow:
    application_id: int
    location_id: str
    turnover: float
    currency: str
    active: bool
    source_row: int


@dataclass
class ParsedReconciliationBase:
    rows: list[BaseRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _base_header_canonical(normalized: str) -> str | None:
    for canonical, aliases in _BASE_HEADER_ALIASES.items():
        if normalized in aliases:
            return canonical
    return None


def _parse_active(raw: str) -> bool | None:
    folded = raw.strip().casefold()
    if folded in _TRUE_VALUES:
        return True
    if folded in _FALSE_VALUES:
        return False
    return None


def _parse_application_id(raw: str) -> int | None:
    folded = raw.strip()
    if not folded:
        return None
    try:
        return int(folded)
    except ValueError:
        try:
            value = float(folded)
        except ValueError:
            return None
        if value.is_integer():
            return int(value)
        return None


def _read_base_rows(
    path: Path, suffix: str
) -> tuple[list[list[str]] | None, str | None]:
    """Read the reconciliation base into a list of rows.

    Returns ``(rows, None)`` where ``rows[0]`` is the normalized header, or
    ``(None, error_message)`` when the file is unreadable or has no header.
    """
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8", errors="ignore") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return None, "CSV file has no header"
            headers = [
                " ".join(f.split()).strip() if f else "" for f in reader.fieldnames
            ]
            data: list[list[str]] = []
            for raw in reader:
                row: list[str] = []
                for f in reader.fieldnames:
                    value = raw.get(f, "") if f else ""
                    row.append("" if value is None else str(value).strip())
                data.append(row)
        return [headers, *data], None
    if suffix == ".xlsx":
        workbook = load_workbook(path, data_only=True, read_only=True)
        if not workbook.worksheets:
            return None, "XLSX file has no sheets"
        sheet = workbook.worksheets[0]
        sheet_rows = list(sheet.iter_rows(values_only=True))
        if not sheet_rows:
            return None, "XLSX file has no header"
        headers = [
            " ".join(str(cell).split()).strip() if cell is not None else ""
            for cell in sheet_rows[0]
        ]
        if not any(headers):
            return None, "XLSX file has no header"
        data = []
        for r in sheet_rows[1:]:
            row = []
            for col_idx in range(len(headers)):
                cell = r[col_idx] if col_idx < len(r) else None
                row.append("" if cell is None else str(cell).strip())
            data.append(row)
        return [headers, *data], None
    return None, f"unsupported file type: {suffix}"


def parse_reconciliation_base(
    path: Path, *, default_currency: str = "USD"
) -> ParsedReconciliationBase:
    """Read the manual reconciliation base an admin uploads per month.

    The file is a one-row-per-(application, location) table that lists every
    Location ID the partner app saw this month, with turnover, currency, and
    an Active flag. CSV (``.csv``) and Excel (``.xlsx``) are supported; the
    parser is tolerant of partial files — bad rows are skipped and recorded
    in ``notes`` — so the function never raises on a single broken row.

    Required fields per row are ``application_id`` and ``location_id``;
    ``turnover`` falls back to ``0.0``, ``currency`` to ``default_currency``,
    and ``active`` to ``False``. ``error`` is set only when the file cannot
    be opened or has no header at all.
    """
    if not path.exists():
        return ParsedReconciliationBase(
            rows=[], notes=[], error=f"file not found: {path}"
        )
    suffix = path.suffix.lower()
    try:
        result, header_error = _read_base_rows(path, suffix)
    except Exception as exc:  # noqa: BLE001 — intake must not crash the month run
        return ParsedReconciliationBase(rows=[], notes=[], error=str(exc))
    if header_error is not None:
        return ParsedReconciliationBase(rows=[], notes=[], error=header_error)
    assert result is not None
    if len(result) < 2:
        return ParsedReconciliationBase(rows=[], notes=[], error="file has no data rows")

    headers = result[0]
    data_rows = result[1:]

    canonical_to_index: dict[str, int] = {}
    for col_idx, header in enumerate(headers):
        if not header:
            continue
        folded = " ".join(header.split()).casefold().strip()
        canonical = _base_header_canonical(folded)
        if canonical is not None and canonical not in canonical_to_index:
            canonical_to_index[canonical] = col_idx

    fallback_currency = (default_currency or "USD").upper()
    rows: list[BaseRow] = []
    notes: list[str] = []

    for offset, raw in enumerate(data_rows):
        source_row = offset + 2  # 1-based, header is row 1

        def _cell(canonical: str) -> str:
            idx = canonical_to_index.get(canonical)
            if idx is None or idx >= len(raw):
                return ""
            value = raw[idx]
            return "" if value is None else str(value).strip()

        app_id_raw = _cell("application_id")
        location_raw = _cell("location_id")
        turnover_raw = _cell("turnover")
        currency_raw = _cell("currency")
        active_raw = _cell("active")

        # Skip rows where every recognised column is empty (separator lines,
        # trailing blank rows, etc.).
        if not (
            app_id_raw
            or location_raw
            or turnover_raw
            or currency_raw
            or active_raw
        ):
            continue

        application_id = _parse_application_id(app_id_raw)
        if application_id is None:
            notes.append(
                f"row {source_row}: missing or invalid Application ID '{app_id_raw}'"
            )
            continue
        if not location_raw:
            notes.append(f"row {source_row}: missing Location ID")
            continue

        turnover = 0.0
        if turnover_raw:
            parsed_turnover = _parse_amount(turnover_raw)
            if parsed_turnover is None:
                notes.append(
                    f"row {source_row}: cannot parse Turnover '{turnover_raw}'"
                )
                continue
            turnover = parsed_turnover

        if currency_raw:
            currency = currency_raw.upper()[:3]
            if len(currency) != 3:
                currency = fallback_currency
        else:
            currency = fallback_currency

        if active_raw:
            parsed_active = _parse_active(active_raw)
            if parsed_active is None:
                notes.append(
                    f"row {source_row}: cannot parse Active '{active_raw}'"
                )
                continue
            active = parsed_active
        else:
            active = False

        rows.append(
            BaseRow(
                application_id=application_id,
                location_id=location_raw,
                turnover=turnover,
                currency=currency,
                active=active,
                source_row=source_row,
            )
        )

    return ParsedReconciliationBase(rows=rows, notes=notes, error=None)


def ingest_dir(folder: Path) -> list[ExtractedReport]:
    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.name not in {".gitkeep", "README.md"} and not p.name.startswith(".")
    )
    return [extract_file(path) for path in files]

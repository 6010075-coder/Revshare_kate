from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from revshare.registry import DEFAULT_REGISTRY, Registry
from revshare.secrets import load_secrets

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
TURNOVER_LABELS = (
    "reported turnover",
    "turnover",
    "gmv",
    "оборот",
    "partner reported",
)
MONTHS = {
    "01": ("jan", "january", "янв"),
    "02": ("feb", "february", "фев"),
    "03": ("mar", "march", "мар"),
    "04": ("apr", "april", "апр"),
    "05": ("may", "май"),
    "06": ("jun", "june", "июн"),
    "07": ("jul", "july", "июл"),
    "08": ("aug", "august", "авг"),
    "09": ("sep", "sept", "september", "сен"),
    "10": ("oct", "october", "окт"),
    "11": ("nov", "november", "ноя"),
    "12": ("dec", "december", "дек"),
}


@dataclass
class SheetWriteResult:
    ok: bool
    cell: str = ""
    was: str | None = None
    written: str | None = None
    error: str = ""


def spreadsheet_id() -> str:
    load_secrets()
    return (
        os.getenv("REVSHARE_GOOGLE_SHEET_ID", "").strip()
        or Registry.load().sheet.get("id", "")
    )


def credentials_ready() -> bool:
    load_secrets()
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        return bool(spreadsheet_id())
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    return bool(path and Path(path).exists() and spreadsheet_id())


def _credentials():
    from google.oauth2 import service_account

    load_secrets()
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if path and Path(path).exists():
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    raise RuntimeError("google_credentials_missing")


@lru_cache(maxsize=1)
def _service():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)


def year_tab(period: str) -> str:
    year = period.split("-")[0]
    tabs = Registry.load().sheet.get("year_tabs") or {}
    tab = tabs.get(int(year)) if year.isdigit() else None
    if isinstance(tab, dict) and tab.get("title"):
        return str(tab["title"])
    return year


def _col_letter(index: int) -> str:
    letters = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _normalize_header(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _month_matches(header: str, period: str) -> bool:
    year, month = period.split("-")
    short_year = year[-2:]
    names = MONTHS[month]
    if month in header or f"{year}-{month}" in header:
        return True
    if year in header and any(name in header for name in names):
        return True
    if short_year in header and any(name in header for name in names):
        return True
    if re.search(rf"\b{int(month)}\b", header) and year in header:
        return True
    return False


def find_month_column(headers: list[list[str]], period: str) -> int | None:
    for row in headers[:8]:
        for index, cell in enumerate(row):
            header = _normalize_header(cell)
            if header and _month_matches(header, period):
                return index
    return None


def find_partner_row(
    rows: list[list[str]], partner_name: str
) -> tuple[int, int] | None:
    target = partner_name.casefold().strip()
    if not target:
        return None
    for row_index, row in enumerate(rows):
        for col_index in range(min(3, len(row))):
            cell = str(row[col_index] if col_index < len(row) else "").strip()
            if cell.casefold() == target:
                return row_index, col_index
    for row_index, row in enumerate(rows):
        for col_index in range(min(3, len(row))):
            cell = str(row[col_index] if col_index < len(row) else "").strip()
            if target in cell.casefold() and len(cell) >= max(4, len(partner_name) - 2):
                return row_index, col_index
    return None


def find_turnover_row(
    rows: list[list[str]], partner_row: int, partner_col: int
) -> int:
    first_cell = str(rows[partner_row][partner_col] if partner_col < len(rows[partner_row]) else "")
    if first_cell.casefold().strip() == first_cell.casefold().strip():
        label = _normalize_header(first_cell)
        if label and not any(token in label for token in TURNOVER_LABELS):
            return partner_row
    for offset in range(0, 6):
        row_index = partner_row + offset
        if row_index >= len(rows):
            break
        label = _normalize_header(rows[row_index][0] if rows[row_index] else "")
        if any(token in label for token in TURNOVER_LABELS):
            return row_index
    return partner_row


def locate_cell(rows: list[list[str]], partner_name: str, period: str) -> tuple[int, int]:
    month_col = find_month_column(rows, period)
    if month_col is None:
        raise RuntimeError(f"month_column_not_found:{period}")
    located = find_partner_row(rows, partner_name)
    if located is None:
        raise RuntimeError(f"partner_row_not_found:{partner_name}")
    partner_row, _partner_col = located
    turnover_row = find_turnover_row(rows, partner_row, _partner_col)
    return turnover_row, month_col


def _read_tab_values(tab: str) -> list[list[str]]:
    service = _service()
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id(),
            range=f"'{tab}'!A1:BZ400",
            valueRenderOption="FORMULA",
        )
        .execute()
    )
    return result.get("values") or []


def _read_cell(tab: str, row: int, col: int) -> str | None:
    cell = f"'{tab}'!{_col_letter(col)}{row + 1}"
    service = _service()
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id(),
            range=cell,
            valueRenderOption="FORMULA",
        )
        .execute()
    )
    values = result.get("values") or []
    if not values or not values[0]:
        return None
    return str(values[0][0])


def write_turnover(
    partner_name: str,
    period: str,
    turnover: float,
    currency: str,
) -> SheetWriteResult:
    if not credentials_ready():
        return SheetWriteResult(ok=False, error="google_credentials_missing")
    tab = year_tab(period)
    rows = _read_tab_values(tab)
    row_index, col_index = locate_cell(rows, partner_name, period)
    cell = f"{_col_letter(col_index)}{row_index + 1}"
    current = _read_cell(tab, row_index, col_index)
    if current and str(current).startswith("="):
        return SheetWriteResult(
            ok=False,
            cell=f"{tab}!{cell}",
            was=current,
            error="formula_cell",
        )
    value = round(float(turnover), 2)
    service = _service()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id(),
        range=f"'{tab}'!{cell}",
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()
    return SheetWriteResult(
        ok=True,
        cell=f"{tab}!{cell}",
        was=current,
        written=f"{value} {currency}".strip(),
    )


def probe_sheet(period: str) -> dict[str, Any]:
    tab = year_tab(period)
    rows = _read_tab_values(tab)
    month_col = find_month_column(rows, period)
    sample_partners = []
    for row in rows[:120]:
        if not row:
            continue
        name = str(row[0]).strip()
        if name and not any(token in name.casefold() for token in TURNOVER_LABELS):
            if len(name) > 2 and not name.isnumeric():
                sample_partners.append(name)
        if len(sample_partners) >= 10:
            break
    return {
        "spreadsheet_id": spreadsheet_id(),
        "tab": tab,
        "period": period,
        "month_column": None if month_col is None else _col_letter(month_col),
        "sample_partners": sample_partners,
        "rows_loaded": len(rows),
    }


def clear_cached_service() -> None:
    _service.cache_clear()

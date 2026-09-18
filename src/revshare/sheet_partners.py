from __future__ import annotations

import json
import re
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from revshare import sheets
from revshare.countries import canonical_app_category
from revshare.registry import Registry

COL_NAME = 0
COL_TYPE = 1
COL_LEGAL = 2
COL_RATE = 34  # column AI (August Rate in the 2026 layout)

RATE_COLUMNS = tuple(range(6, 35, 4))  # monthly Rate columns Jan..Aug (G, K, O, ...)

SECTION_NAMES = frozenset(
    name.casefold()
    for name in (
        "Total",
        "Chatbots",
        "Other",
        "SMS",
        "Europe (Hungary) - Global",
        "Armenia - Global",
        "Ukraine",
        "KZ - Global",
        "Brazil",
        "Cyprus",
    )
)

FX_MARKERS = (
    "руб =",
    "евро =",
    "гривен",
    "тенге =",
    "средний курс",
)


@dataclass
class SheetPartnerRow:
    name: str
    legal_name: str
    rate: float | None
    category: str


@dataclass
class PartnerImportResult:
    created: list[str]
    updated: list[str]
    skipped: list[str]


def _cell(row: list[Any], index: int) -> str:
    if index >= len(row):
        return ""
    return str(row[index] or "").strip()


def parse_rate(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold().replace("%", "").strip()
    if "/" in lowered:
        parts = re.findall(r"[\d.]+", lowered)
        if not parts:
            return None
        numbers = [float(part) for part in parts]
        return max(numbers) / 100
    if re.fullmatch(r"[\d.]+", lowered):
        number = float(lowered)
        if number > 100:
            return None
        if number > 1:
            return number / 100
        return number
    match = re.search(r"([\d.]+)", lowered)
    if not match:
        return None
    number = float(match.group(1))
    if number > 100:
        return None
    if number > 1:
        return number / 100
    return number


def parse_legal_country(legal_name: str) -> str:
    match = re.search(r"\(([^)]+)\)\s*$", legal_name.strip())
    if not match:
        return ""
    country = match.group(1).strip()
    aliases = {
        "kz": "Kazakhstan",
        "укр": "Ukraine",
        "ukr": "Ukraine",
        "ukraine": "Ukraine",
        "эстония": "Estonia",
        "usa": "United States",
        "rf": "Russia",
        "рф": "Russia",
        "kyrgystan": "Kyrgyzstan",
        "киргизия": "Kyrgyzstan",
        "cypris": "Cyprus",
        "moldova": "Moldova",
        "global": "Global",
    }
    return aliases.get(country.casefold(), country)


def is_section_row(name: str, row: list[Any]) -> bool:
    normalized = name.casefold().strip()
    if not normalized:
        return True
    if normalized == "name" and _cell(row, COL_TYPE).casefold() == "type":
        return True
    if normalized in SECTION_NAMES:
        return True
    if normalized == "total":
        return True
    lowered = name.casefold()
    if any(marker in lowered for marker in FX_MARKERS):
        return True
    if normalized.endswith(" - global") and not _cell(row, COL_TYPE):
        return True
    return False


def partner_rate_from_row(row: list[Any]) -> float | None:
    for index in [COL_RATE, *reversed(RATE_COLUMNS)]:
        rate = parse_rate(_cell(row, index))
        if rate is not None:
            return rate
    return None


def iter_partners_from_rows(rows: list[list[Any]]) -> list[SheetPartnerRow]:
    partners: list[SheetPartnerRow] = []
    for row in rows:
        name = _cell(row, COL_NAME)
        if is_section_row(name, row):
            continue
        legal_name = _cell(row, COL_LEGAL)
        partners.append(
            SheetPartnerRow(
                name=name,
                legal_name=legal_name,
                rate=partner_rate_from_row(row),
                category=canonical_app_category(_cell(row, COL_TYPE)),
            )
        )
    return partners


def _gviz_rows(spreadsheet_id: str, tab: str, range_a1: str) -> list[list[Any]]:
    url = (
        "https://docs.google.com/spreadsheets/d/"
        f"{spreadsheet_id}/gviz/tq?tqx=out:json"
        f"&sheet={urllib.parse.quote(tab, safe='')}&range={urllib.parse.quote(range_a1, safe='')}"
    )
    raw = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
    payload = json.loads(
        raw.removeprefix("/*O_o*/\n")
        .removeprefix("google.visualization.Query.setResponse(")
        .rstrip(");")
    )
    rows: list[list[Any]] = []
    for row in payload["table"].get("rows") or []:
        values: list[Any] = []
        for cell in row.get("c") or []:
            if cell is None:
                values.append("")
                continue
            value = cell.get("v")
            if value in (None, "") and cell.get("f"):
                value = cell.get("f")
            values.append(value if value is not None else "")
        rows.append(values)
    return rows


def _fetch_partner_rows_public(tab: str, spreadsheet_id: str) -> list[list[Any]]:
    rows = _gviz_rows(spreadsheet_id, tab, "A1:AH150")
    rate_rows = _gviz_rows(spreadsheet_id, tab, "AI1:AI150")
    for index, row in enumerate(rows):
        rate_value = ""
        if index < len(rate_rows) and rate_rows[index]:
            rate_value = rate_rows[index][0]
        while len(row) <= COL_RATE:
            row.append("")
        row[COL_RATE] = rate_value
    return rows


def fetch_partner_rows(year: int | str = 2026) -> list[list[Any]]:
    tab = sheets.year_tab(f"{year}-01")
    if sheets.credentials_ready():
        return sheets._read_tab_values(tab)
    spreadsheet_id = sheets.spreadsheet_id() or Registry.load().sheet.get("id", "")
    if not spreadsheet_id:
        raise RuntimeError("spreadsheet_id_missing")
    return _fetch_partner_rows_public(tab, spreadsheet_id)


def import_partners_from_sheet(
    store,
    *,
    year: int | str = 2026,
    rows: list[list[Any]] | None = None,
    dry_run: bool = False,
) -> PartnerImportResult:
    loaded = rows if rows is not None else fetch_partner_rows(year)
    sheet_partners = iter_partners_from_rows(loaded)
    with store.connect() as db:
        existing = {
            str(row["name"]).casefold().strip(): dict(row)
            for row in db.execute("SELECT * FROM partners WHERE active = 1")
        }
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for partner in sheet_partners:
        key = partner.name.casefold().strip()
        current = existing.get(key)
        if current is None:
            if dry_run:
                created.append(partner.name)
                continue
            store.add_partner(
                name=partner.name,
                legal_name=partner.legal_name,
                legal_country=parse_legal_country(partner.legal_name),
                altegio_legal_entity="",
                contact_name="",
                email="",
                telegram="",
                report_channel="email",
                token=secrets.token_urlsafe(18),
                rate=partner.rate,
                currency="USD",
                category=partner.category,
            )
            created.append(partner.name)
            continue

        if dry_run:
            updated.append(partner.name)
            continue

        with store.connect() as db:
            db.execute(
                """
                UPDATE partners
                SET name = ?, legal_name = ?, legal_country = ?, rate = ?, category = ?
                WHERE id = ?
                """,
                (
                    partner.name,
                    partner.legal_name,
                    parse_legal_country(partner.legal_name),
                    partner.rate,
                    partner.category,
                    current["id"],
                ),
            )
        updated.append(partner.name)

    return PartnerImportResult(created=created, updated=updated, skipped=skipped)

"""Tests for ``parse_reconciliation_base`` (step 1 data layer)."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from revshare.extract import BaseRow, ParsedReconciliationBase, parse_reconciliation_base


_CSV_HEADER = (
    "Application ID,Location ID,Turnover,Currency,Active\n"
    "101,12345,1000.50,USD,1\n"
    "101,12346,500,usd,true\n"
    "101,12347,250.75,eur,yes\n"
    "102,12348,,EUR,да\n"
)


def _write_csv(path: Path) -> Path:
    path.write_text(_CSV_HEADER, encoding="utf-8")
    return path


def _build_xlsx(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Application ID", "Location ID", "Turnover", "Currency", "Active"])
    sheet.append([101, "12345", 1000.50, "USD", 1])
    sheet.append([101, "12346", 500, "USD", "true"])
    sheet.append([102, "12347", 0, "EUR", "да"])
    workbook.save(path)
    return path


def test_parse_reconciliation_base_csv_clean(tmp_path: Path):
    path = _write_csv(tmp_path / "base.csv")

    parsed = parse_reconciliation_base(path)

    assert isinstance(parsed, ParsedReconciliationBase)
    assert parsed.error is None
    assert parsed.notes == []
    assert parsed.rows == [
        BaseRow(application_id=101, location_id="12345", turnover=1000.50,
                currency="USD", active=True, source_row=2),
        BaseRow(application_id=101, location_id="12346", turnover=500.0,
                currency="USD", active=True, source_row=3),
        BaseRow(application_id=101, location_id="12347", turnover=250.75,
                currency="EUR", active=True, source_row=4),
        BaseRow(application_id=102, location_id="12348", turnover=0.0,
                currency="EUR", active=True, source_row=5),
    ]


def test_parse_reconciliation_base_xlsx(tmp_path: Path):
    path = _build_xlsx(tmp_path / "base.xlsx")

    parsed = parse_reconciliation_base(path)

    assert parsed.error is None
    assert parsed.notes == []
    assert [r.location_id for r in parsed.rows] == ["12345", "12346", "12347"]
    assert [r.active for r in parsed.rows] == [True, True, True]
    assert parsed.rows[2].turnover == 0


def test_parse_reconciliation_base_active_variants(tmp_path: Path):
    path = tmp_path / "active.csv"
    path.write_text(
        "App ID,Location ID,Turnover,Currency,Active\n"
        "1,L1,10,USD,1\n"
        "1,L2,10,USD,true\n"
        "1,L3,10,USD,yes\n"
        "1,L4,10,USD,да\n"
        "1,L5,10,USD,0\n"
        "1,L6,10,USD,no\n",
        encoding="utf-8",
    )

    parsed = parse_reconciliation_base(path)

    assert parsed.error is None
    assert parsed.notes == []
    assert [r.active for r in parsed.rows] == [True, True, True, True, False, False]
    assert [r.location_id for r in parsed.rows] == ["L1", "L2", "L3", "L4", "L5", "L6"]


def test_parse_reconciliation_base_skips_missing_location(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "Application ID,Location ID,Turnover,Currency,Active\n"
        "1,GOOD,100,USD,1\n"
        "2,,50,USD,1\n"
        "3,ALSO_GOOD,75,USD,1\n",
        encoding="utf-8",
    )

    parsed = parse_reconciliation_base(path)

    assert parsed.error is None
    assert [r.location_id for r in parsed.rows] == ["GOOD", "ALSO_GOOD"]
    assert len(parsed.notes) == 1
    assert "row 3" in parsed.notes[0]
    assert "Location ID" in parsed.notes[0]


def test_parse_reconciliation_base_returns_error_for_missing_file(tmp_path: Path):
    parsed = parse_reconciliation_base(tmp_path / "nope.csv")

    assert parsed.error is not None
    assert parsed.rows == []
    assert parsed.notes == []


def test_parse_reconciliation_base_returns_error_for_empty_csv(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    parsed = parse_reconciliation_base(path)

    assert parsed.error is not None
    assert parsed.rows == []
    assert parsed.notes == []


def test_parse_reconciliation_base_lowercases_and_trims_headers(tmp_path: Path):
    path = tmp_path / "headers.csv"
    path.write_text(
        "  application ID  , LOCATION id ,Turnover,  currency ,active\n"
        "1,L1,100,USD,1\n",
        encoding="utf-8",
    )

    parsed = parse_reconciliation_base(path)

    assert parsed.error is None
    assert parsed.notes == []
    assert parsed.rows[0].location_id == "L1"
    assert parsed.rows[0].turnover == 100
    assert parsed.rows[0].active is True


def test_parse_reconciliation_base_falls_back_to_default_currency(tmp_path: Path):
    path = tmp_path / "no_currency.csv"
    path.write_text(
        "Application ID,Location ID,Turnover,Active\n"
        "1,L1,100,1\n",
        encoding="utf-8",
    )

    parsed = parse_reconciliation_base(path, default_currency="EUR")

    assert parsed.error is None
    assert parsed.rows[0].currency == "EUR"


def test_parse_reconciliation_base_skips_fully_blank_rows(tmp_path: Path):
    path = tmp_path / "blanks.csv"
    path.write_text(
        "Application ID,Location ID,Turnover,Currency,Active\n"
        "1,L1,100,USD,1\n"
        ",,,,\n"
        "2,L2,200,USD,0\n",
        encoding="utf-8",
    )

    parsed = parse_reconciliation_base(path)

    assert parsed.error is None
    assert len(parsed.rows) == 2
    assert [r.location_id for r in parsed.rows] == ["L1", "L2"]
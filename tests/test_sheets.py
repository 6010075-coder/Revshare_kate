from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from revshare import sheets
from revshare.sheet_sync import flush_queued_sheet_writes
from revshare.store import Store


SAMPLE_ROWS = [
    ["Partner", "Aug 2026", "Sep 2026"],
    ["Message.help", "1000", "1100"],
    ["Reported turnover", "", ""],
    ["Flowsell", "500", ""],
]


def test_find_month_column_matches_english_month():
    assert sheets.find_month_column(SAMPLE_ROWS, "2026-08") == 1
    assert sheets.find_month_column(SAMPLE_ROWS, "2026-09") == 2


def test_find_partner_row_exact_and_partial():
    assert sheets.find_partner_row(SAMPLE_ROWS, "Message.help") == (1, 0)
    assert sheets.find_partner_row(SAMPLE_ROWS, "Flowsell") == (3, 0)
    assert sheets.find_partner_row(SAMPLE_ROWS, "Unknown") is None


def test_locate_cell_returns_turnover_row_and_month_column():
    row, col = sheets.locate_cell(SAMPLE_ROWS, "Message.help", "2026-08")
    assert row == 1
    assert col == 1


def test_find_turnover_row_uses_fallback_when_partner_cell_is_empty():
    rows = [["", "Aug 2026"], ["", ""], ["Reported turnover", ""]]
    assert sheets.find_turnover_row(rows, 1, 0) == 2


def test_write_turnover_skips_formula_cells(monkeypatch):
    monkeypatch.setenv("REVSHARE_GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"client_email":"x@y.z"}')
    sheets.clear_cached_service()

    with patch.object(sheets, "_read_tab_values", return_value=SAMPLE_ROWS):
        with patch.object(sheets, "_read_cell", return_value="=B1*0.2"):
            result = sheets.write_turnover("Message.help", "2026-08", 1200, "USD")

    assert not result.ok
    assert result.error == "formula_cell"
    assert result.cell == "2026!B2"


def test_write_turnover_updates_value(monkeypatch):
    monkeypatch.setenv("REVSHARE_GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"client_email":"x@y.z"}')
    sheets.clear_cached_service()

    service = MagicMock()
    with patch.object(sheets, "_read_tab_values", return_value=SAMPLE_ROWS):
        with patch.object(sheets, "_read_cell", return_value="1000"):
            with patch.object(sheets, "_service", return_value=service):
                result = sheets.write_turnover("Message.help", "2026-08", 1200, "USD")

    assert result.ok
    assert result.cell == "2026!B2"
    assert result.was == "1000"
    service.spreadsheets().values().update.assert_called_once()


def test_flush_queued_sheet_writes_updates_store(tmp_path, monkeypatch):
    db_path = tmp_path / "revshare.db"
    store = Store(db_path)
    partner_id = store.add_partner(
        name="Message.help",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        rate=0.15,
        currency="USD",
        token="token",
    )
    store.replace_usage(partner_id, "2026-08", connected_ids=["101"], active_ids=["101"])
    report_id, _ = store.submit_report(
        partner_id=partner_id,
        period="2026-08",
        turnover=1000,
        currency="USD",
        reported_share=150,
        location_ids=["101"],
        comment="",
        attachment=None,
    )
    store.approve_report(report_id)
    store.mark_posted(report_id, "test")

    monkeypatch.setenv("REVSHARE_GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        json.dumps({"client_email": "writer@project.iam.gserviceaccount.com"}),
    )
    sheets.clear_cached_service()

    write_result = sheets.SheetWriteResult(
        ok=True,
        cell="2026!B2",
        was="900",
        written="1000 USD",
    )
    with patch.object(sheets, "write_turnover", return_value=write_result):
        results = flush_queued_sheet_writes(store)

    assert len(results) == 1
    assert results[0]["ok"]
    writes = store.sheet_writes("2026-08")
    assert writes[0]["status"] == "written"
    assert writes[0]["sheet_cell"] == "2026!B2"


def test_credentials_ready_requires_sheet_id(monkeypatch, tmp_path):
    key_file = tmp_path / "sa.json"
    key_file.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key_file))
    monkeypatch.delenv("REVSHARE_GOOGLE_SHEET_ID", raising=False)
    sheets.clear_cached_service()

    with patch.object(sheets.Registry, "load") as load:
        load.return_value.sheet = {"id": ""}
        assert not sheets.credentials_ready()

    with patch.object(sheets.Registry, "load") as load:
        load.return_value.sheet = {"id": "sheet-id"}
        assert sheets.credentials_ready()

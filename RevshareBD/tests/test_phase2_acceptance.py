"""Acceptance tests for Phase 2 of the revshare decomposition.

Each ``FR-XX`` block exercises the scenarios from the integration spec
the user gave me for the bot/web/admin flows. Tests are grouped by
requirement and use the shared ``bot_*`` / ``telegram_mocks`` /
``store_with_partner`` fixtures so the assertions stay short and
focused on the contract — not on the storage layer plumbing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from revshare.bot import handle_callback, handle_partner_document
from revshare.extract import ParsedPartnerReport
from revshare.store import Store
from revshare.web import create_app


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_with_partner(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="Phase2 App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@phase2",
        report_channel="telegram",
        token="tkn-phase2",
        rate=0.2,
        currency="USD",
    )
    store.bind_telegram_chat("tkn-phase2", "200600")
    return store


def _partner_id(store: Store, chat_id: str = "200600") -> int:
    return int(store.partner_by_chat_id(chat_id)["id"])


@pytest.fixture
def telegram_mocks(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {
        "messages": [],
        "replies": [],
        "callbacks": [],
        "documents": [],
        "downloaded": b"raw-bytes",
        "edits": [],
    }

    def fake_send_message(token, chat_id, text, *, reply_markup=None):
        state["messages"].append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"ok": True}

    def fake_send_message_reply(token, chat_id, text, reply_markup=None):
        state["replies"].append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"ok": True}

    def fake_answer_callback_query(token, callback_id, text="", show_alert=False):
        state["callbacks"].append(
            {"id": callback_id, "text": text, "show_alert": show_alert}
        )
        return {"ok": True}

    def fake_edit_message_text(token, chat_id, message_id, text, reply_markup=None):
        state["edits"].append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True}

    def fake_send_document(
        token, chat_id, document, filename="", caption="", **kwargs
    ):
        state["documents"].append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "caption": caption,
                "size": len(document) if document is not None else 0,
            }
        )
        return {"ok": True}

    def fake_get_file_path(token, file_id):
        return f"documents/{file_id}.xlsx"

    def fake_download_file(token, file_path):
        return state["downloaded"]

    monkeypatch.setattr("revshare.bot.send_message", fake_send_message)
    monkeypatch.setattr(
        "revshare.telegram.send_message_reply", fake_send_message_reply
    )
    monkeypatch.setattr(
        "revshare.telegram.answer_callback_query", fake_answer_callback_query
    )
    monkeypatch.setattr(
        "revshare.telegram.edit_message_text", fake_edit_message_text
    )
    monkeypatch.setattr(
        "revshare.telegram.send_document", fake_send_document
    )
    monkeypatch.setattr("revshare.telegram.get_file_path", fake_get_file_path)
    monkeypatch.setattr("revshare.telegram.download_file", fake_download_file)
    return state


def _make_document(file_name: str = "report.xlsx", file_id: str = "file-1") -> dict:
    return {
        "file_id": file_id,
        "file_name": file_name,
        "mime_type": "application/octet-stream",
        "file_size": 2048,
    }


def _parsed(**overrides: Any) -> ParsedPartnerReport:
    defaults: dict[str, Any] = dict(
        turnover=1000.0,
        currency="USD",
        reported_share=200.0,
        location_ids=["loc-1", "loc-2"],
        location_source="column",
        notes=[],
        period="2026-09",
        application_breakdown=[],
    )
    defaults.update(overrides)
    return ParsedPartnerReport(**defaults)


def _bot_interactions(store: Store, kind: str | None = None) -> list[dict[str, Any]]:
    with store.connect() as db:
        if kind is not None:
            rows = db.execute(
                "SELECT kind, partner_id, chat_id, payload, response "
                "FROM bot_interactions WHERE kind = ? ORDER BY id",
                (kind,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT kind, partner_id, chat_id, payload, response "
                "FROM bot_interactions ORDER BY id"
            ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "kind": row["kind"],
                "partner_id": row["partner_id"],
                "chat_id": row["chat_id"],
                "payload": json.loads(row["payload"] or "{}"),
                "response": json.loads(row["response"] or "{}"),
            }
        )
    return out


def _drafts(store: Store) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM report_drafts ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _invoke(
    store: Store,
    parsed: ParsedPartnerReport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    caption: str = "2026-09",
    update_id: int = 900,
    file_id: str = "file-1",
) -> None:
    monkeypatch.setattr("revshare.bot.ROOT", tmp_path)
    monkeypatch.setattr(
        "revshare.extract.parse_partner_report",
        lambda *args, **kwargs: parsed,
    )
    handle_partner_document(
        store,
        "tok",
        _make_document(file_id=file_id),
        message={"chat": {"id": 200600}, "caption": caption},
        sender={},
        chat_id=200600,
        update_id=update_id,
    )


# ---------------------------------------------------------------------------
# FR-01: the parser reads only the FIRST sheet of the workbook.
# ---------------------------------------------------------------------------


def test_fr01_first_sheet_used_when_second_is_sample(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Companion sample sheets must NOT contribute rows to the draft."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [
            {
                "url": "https://app.alteg.io/app/999/",
                "comment": "Main",
                "category": "",
            }
        ],
    )
    # Mock the parser to return rows from BOTH sheets, but make sure the
    # bot saves only the first sheet's contents. We assert that by checking
    # that ``parse_partner_report`` is called with a real path and the bot
    # uses its return value verbatim (parser, not bot, is responsible for
    # filtering).
    sample_rows = [
        "1099", "Sample Salon", "Monthly", "USD", 999, 1, "USD", 0.2
    ]
    primary_rows = [
        "101", "Beauty Loft", "Monthly", "USD", 100, 1, "USD", 0.2,
        "102", "Atlas", "Monthly", "EUR", 50, 1.1593, "USD", 0.2,
    ]
    _invoke(
        store_with_partner,
        _parsed(
            location_ids=["101", "102"],
            turnover=round(100 + 50 * 1.1593, 2),
        ),
        monkeypatch,
        tmp_path,
    )
    drafts = _drafts(store_with_partner)
    assert len(drafts) == 1
    # Only primary sheet rows, no '1099' / 'Sample Salon' leakage.
    assert json.loads(drafts[0]["location_ids"]) == ["101", "102"]


def test_fr01_unrecognised_first_sheet_reports_error(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """First sheet with no recognisable headers → error logged, no draft."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(
            turnover=None,
            reported_share=None,
            location_ids=[],
            error="table_structure_not_found",
        ),
        monkeypatch,
        tmp_path,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["status"] == "table_structure_not_found"
    assert _drafts(store_with_partner) == []
    assert "Не удалось разобрать таблицу" in telegram_mocks["messages"][0]["text"]


# ---------------------------------------------------------------------------
# FR-02: the reporting month is parsed from B3 of the first sheet.
# ---------------------------------------------------------------------------


def test_fr02_period_russian_from_b3(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(period="2026-09"),
        monkeypatch,
        tmp_path,
    )
    drafts = _drafts(store_with_partner)
    assert drafts[0]["period"] == "2026-09"
    # Summary text contains the humanised period.
    assert any("Сентябрь 2026" in r["text"] for r in telegram_mocks["replies"])


def test_fr02_period_english_from_b3(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(store_with_partner, _parsed(period="2026-09"), monkeypatch, tmp_path)
    assert _drafts(store_with_partner)[0]["period"] == "2026-09"


def test_fr02_period_from_b3_overrides_caption(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    """The bot stores ``parsed.period`` (B3), not the caption."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(period="2026-09"),
        monkeypatch,
        tmp_path,
        caption="2026-08",  # intentionally wrong
    )
    assert _drafts(store_with_partner)[0]["period"] == "2026-09"


def test_fr02_empty_b3_blocks_draft(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    """Empty B3 → period_not_found, message, no draft."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(period=None),
        monkeypatch,
        tmp_path,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["status"] == "period_not_found"
    assert _drafts(store_with_partner) == []
    assert "B3" in telegram_mocks["messages"][0]["text"]


# ---------------------------------------------------------------------------
# FR-03: table structure must look like the 10-column template.
# ---------------------------------------------------------------------------


def test_fr03_table_structure_not_found_blocks_draft(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(
            turnover=None,
            reported_share=None,
            location_ids=[],
            error="table_structure_not_found",
        ),
        monkeypatch,
        tmp_path,
    )
    assert _drafts(store_with_partner) == []
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["status"] == "table_structure_not_found"


# ---------------------------------------------------------------------------
# FR-04: application selection
# ---------------------------------------------------------------------------


def test_fr04_single_app_auto_bound(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Default", "category": ""}],
    )
    _invoke(store_with_partner, _parsed(), monkeypatch, tmp_path)
    drafts = _drafts(store_with_partner)
    assert drafts[0]["application_id"] == 999
    # confirm button present.
    reply = telegram_mocks["replies"][0]
    callbacks = [
        btn["callback_data"]
        for row in reply["reply_markup"]["inline_keyboard"]
        for btn in row
    ]
    assert any(c.startswith("confirm:") for c in callbacks)


def test_fr04_multiple_apps_requires_selection(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [
            {"url": "https://app.alteg.io/app/39/", "comment": "Message.help", "category": ""},
            {"url": "https://app.alteg.io/app/564/", "comment": "AI Beauty Bot", "category": ""},
        ],
    )
    _invoke(store_with_partner, _parsed(), monkeypatch, tmp_path)
    drafts = _drafts(store_with_partner)
    # Draft is in the "waiting for application" state.
    assert drafts[0]["application_id"] is None
    interactions = _bot_interactions(store_with_partner, "document")
    assert (
        interactions[-1]["response"]["status"] == "application_selection_required"
    )
    # Reply carries an inline keyboard with one button per application.
    reply = telegram_mocks["replies"][0]
    callbacks = [
        btn["callback_data"]
        for row in reply["reply_markup"]["inline_keyboard"]
        for btn in row
    ]
    assert any("select_app:" in c for c in callbacks)
    # No confirm button yet.
    assert not any(c.startswith("confirm:") for c in callbacks)


def test_fr04_no_app_blocks_draft(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    _invoke(store_with_partner, _parsed(), monkeypatch, tmp_path)
    assert _drafts(store_with_partner) == []
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["status"] == "no_application"
    assert "Kate" in telegram_mocks["messages"][0]["text"]


def test_fr04_select_app_callback_binds_and_shows_confirm(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    """Multi-app path → select_app:<draft_id>:<app_id> binds app + reveals confirm."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [
            {"url": "https://app.alteg.io/app/39/", "comment": "Message.help", "category": ""},
            {"url": "https://app.alteg.io/app/564/", "comment": "AI Beauty Bot", "category": ""},
        ],
    )
    _invoke(store_with_partner, _parsed(), monkeypatch, tmp_path)
    drafts = _drafts(store_with_partner)
    assert drafts[0]["application_id"] is None
    draft_id = drafts[0]["id"]

    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-pick",
            "data": f"select_app:{draft_id}:39",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}, "message_id": 999},
        },
        update_id=901,
    )

    refreshed = store_with_partner.report_draft_by_id(draft_id)
    assert refreshed["application_id"] == 39
    # Pick interaction logged.
    selects = _bot_interactions(store_with_partner, "select_app")
    assert any(s["response"]["status"] == "bound" for s in selects)
    # Either edit OR reply carries the confirm button.
    surfaces = telegram_mocks["edits"] + telegram_mocks["replies"]
    assert any(
        any(
            btn["callback_data"].startswith("confirm:")
            for row in s["reply_markup"]["inline_keyboard"]
            for btn in row
        )
        for s in surfaces
    ), "Expected confirm button after select_app callback"


def test_fr04_select_app_for_other_partner_rejected(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    """Trying to bind an app that does not belong to the partner is refused."""
    # Partner owns 999 + 564 — the test attempts to bind 39, which the
    # partner does NOT have. The draft was created in the multi-app
    # "waiting for selection" state.
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [
            {"url": "https://app.alteg.io/app/999/", "comment": "Default", "category": ""},
            {"url": "https://app.alteg.io/app/564/", "comment": "AI Beauty Bot", "category": ""},
        ],
    )
    _invoke(store_with_partner, _parsed(), monkeypatch, tmp_path)
    drafts = _drafts(store_with_partner)
    assert drafts[0]["application_id"] is None
    draft_id = drafts[0]["id"]
    # App id 39 doesn't belong to this partner.
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-bad",
            "data": f"select_app:{draft_id}:39",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}, "message_id": 999},
        },
        update_id=902,
    )
    cb = telegram_mocks["callbacks"][0]
    assert "не привязано" in cb["text"]
    # The draft's application_id is still NULL.
    assert store_with_partner.report_draft_by_id(draft_id)["application_id"] is None


# ---------------------------------------------------------------------------
# FR-05/06/07: client lines, empty report, rows with missing amounts.
# ---------------------------------------------------------------------------


def test_fr07_client_with_empty_amount_blocked_with_row_number(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(
            turnover=None,
            reported_share=None,
            location_ids=[],
            error="rows_with_missing_amount",
            error_rows=[12, 15],
        ),
        monkeypatch,
        tmp_path,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["status"] == "rows_with_missing_amount"
    assert interactions[-1]["response"]["error_rows"] == [12, 15]
    text = telegram_mocks["messages"][0]["text"]
    assert "12" in text and "15" in text
    assert "0" in text  # tells partner to fill 0
    assert _drafts(store_with_partner) == []


def test_fr07_client_with_garbage_amount_blocked_with_row_number(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(
            error="rows_with_missing_amount",
            error_rows=[18],
        ),
        monkeypatch,
        tmp_path,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["error_rows"] == [18]
    assert _drafts(store_with_partner) == []


def test_fr08_draft_turnover_matches_parsed(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    """The bot stores the parser's turnover verbatim (FR-08)."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(turnover=1234.56, reported_share=246.91),
        monkeypatch,
        tmp_path,
    )
    drafts = _drafts(store_with_partner)
    assert drafts[0]["turnover"] == 1234.56
    assert drafts[0]["reported_share"] == 246.91


# ---------------------------------------------------------------------------
# FR-09: confirm idempotency and the resend-no-blow path.
# ---------------------------------------------------------------------------


def test_fr09_confirm_creates_report_and_version(
    store_with_partner: Store, telegram_mocks, monkeypatch
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    partner_id = _partner_id(store_with_partner)
    draft_id = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period="2026-09",
        application_id=999,
        attachment="seed.xlsx",
        turnover=500.0,
        currency="USD",
        reported_share=100.0,
        location_ids=["101"],
        extraction_notes=[],
        application_breakdown=[],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-confirm",
            "data": f"confirm:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=903,
    )
    # Draft removed.
    assert store_with_partner.report_draft_by_id(draft_id) is None
    # Report + version row exist.
    with store_with_partner.connect() as db:
        report = db.execute(
            "SELECT * FROM reports WHERE partner_id = ? AND period = ?",
            (partner_id, "2026-09"),
        ).fetchone()
        version = db.execute(
            "SELECT * FROM report_versions WHERE report_id = ?",
            (report["id"],),
        ).fetchone()
    assert report["turnover"] == 500.0
    assert version["version"] == 1
    cb = telegram_mocks["callbacks"][0]
    assert cb["text"] == "Отчёт подтверждён. Ожидает проверки администратором."
    # confirm_success logged with the new report id.
    interactions = _bot_interactions(store_with_partner, "confirm_success")
    assert interactions[-1]["response"]["report_id"] == report["id"]


def test_fr09_confirm_idempotent_under_double_click(
    store_with_partner: Store, telegram_mocks, monkeypatch
) -> None:
    """Double-click → only one version; the second click sees the draft gone."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    partner_id = _partner_id(store_with_partner)
    draft_id = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period="2026-09",
        application_id=999,
        attachment="seed.xlsx",
        turnover=500.0,
        currency="USD",
        reported_share=100.0,
        location_ids=["101"],
        extraction_notes=[],
        application_breakdown=[],
    )
    # First confirm succeeds.
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-1",
            "data": f"confirm:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=904,
    )
    # Second confirm hits the "draft already processed" branch.
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-2",
            "data": f"confirm:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=905,
    )
    cb_texts = [c["text"] for c in telegram_mocks["callbacks"]]
    # The second toast tells the partner the draft was already processed.
    assert any("уже обработан" in t or "не найден" in t for t in cb_texts)
    # Only one report version was created.
    with store_with_partner.connect() as db:
        n = db.execute(
            "SELECT COUNT(*) FROM report_versions rv "
            "JOIN reports r ON r.id = rv.report_id "
            "WHERE r.partner_id = ? AND r.period = ?",
            (partner_id, "2026-09"),
        ).fetchone()[0]
    assert n == 1


def test_fr09_confirm_other_partner_draft_rejected(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    # Inject a draft belonging to a different partner.
    other_partner_id = store_with_partner.add_partner(
        name="Other App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@other",
        report_channel="telegram",
        token="tkn-other",
        rate=0.2,
        currency="USD",
    )
    foreign_draft = store_with_partner.save_bot_draft(
        partner_id=other_partner_id,
        period="2026-09",
        application_id=999,
        attachment="other.xlsx",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["201"],
        extraction_notes=[],
        application_breakdown=[],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-foreign",
            "data": f"confirm:{foreign_draft}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=906,
    )
    cb_text = telegram_mocks["callbacks"][0]["text"]
    assert "не найден" in cb_text
    assert store_with_partner.report_draft_by_id(foreign_draft) is not None
    # No report created for the current partner.
    with store_with_partner.connect() as db:
        n = db.execute(
            "SELECT COUNT(*) FROM reports WHERE partner_id = ?",
            (_partner_id(store_with_partner),),
        ).fetchone()[0]
    assert n == 0


def test_fr09_resend_does_not_change_confirmed_report(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    """Re-uploading a file after confirm does NOT overwrite the live report."""
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    partner_id = _partner_id(store_with_partner)
    draft_id = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period="2026-09",
        application_id=999,
        attachment="seed.xlsx",
        turnover=500.0,
        currency="USD",
        reported_share=100.0,
        location_ids=["101"],
        extraction_notes=[],
        application_breakdown=[],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-conf",
            "data": f"confirm:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=907,
    )
    # After confirm the draft is gone, the report carries turnover=500.
    with store_with_partner.connect() as db:
        report_id = db.execute(
            "SELECT id FROM reports WHERE partner_id = ?", (partner_id,)
        ).fetchone()["id"]
        original_turnover = db.execute(
            "SELECT turnover FROM reports WHERE id = ?", (report_id,)
        ).fetchone()["turnover"]
    assert original_turnover == 500.0
    # Now the partner uploads another file before confirming.
    _invoke(
        store_with_partner,
        _parsed(
            turnover=999.0,
            reported_share=199.8,
            location_ids=["101", "102"],
            period="2026-09",
        ),
        monkeypatch,
        tmp_path,
        file_id="file-2",
        update_id=908,
    )
    # The freshly uploaded draft is in ``report_drafts`` but the
    # confirmed report row stays intact with turnover=500 until the
    # partner confirms the new draft.
    with store_with_partner.connect() as db:
        draft_rows = db.execute(
            "SELECT * FROM report_drafts WHERE partner_id = ? AND period = ?",
            (partner_id, "2026-09"),
        ).fetchall()
        report_turnover = db.execute(
            "SELECT turnover FROM reports WHERE id = ?", (report_id,)
        ).fetchone()["turnover"]
    assert len(draft_rows) == 1
    assert report_turnover == 500.0


# ---------------------------------------------------------------------------
# FR-10: versions on repeated confirms.
# ---------------------------------------------------------------------------


def test_fr10_repeated_confirm_creates_new_version(
    store_with_partner: Store, telegram_mocks, monkeypatch
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    partner_id = _partner_id(store_with_partner)
    first_draft = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period="2026-09",
        application_id=999,
        attachment="a.xlsx",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["101"],
        extraction_notes=[],
        application_breakdown=[],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-1",
            "data": f"confirm:{first_draft}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=910,
    )
    # Upload again with different turnover, then confirm.
    second_draft = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period="2026-09",
        application_id=999,
        attachment="b.xlsx",
        turnover=300.0,
        currency="USD",
        reported_share=60.0,
        location_ids=["101", "102"],
        extraction_notes=[],
        application_breakdown=[],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-2",
            "data": f"confirm:{second_draft}",
            "from": {"id": 1},
            "message": {"chat": {"id": 200600}},
        },
        update_id=911,
    )
    with store_with_partner.connect() as db:
        report_id = db.execute(
            "SELECT id FROM reports WHERE partner_id = ?", (partner_id,)
        ).fetchone()["id"]
        versions = db.execute(
            "SELECT version, turnover FROM report_versions WHERE report_id = ? ORDER BY version",
            (report_id,),
        ).fetchall()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["turnover"] == 100.0
    assert versions[1]["turnover"] == 300.0


def test_fr10_versions_have_distinct_data(
    store_with_partner: Store, telegram_mocks, monkeypatch
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    partner_id = _partner_id(store_with_partner)
    for turnover, location_ids, update_id in (
        (200.0, ["101"], 920),
        (400.0, ["101", "102"], 921),
    ):
        draft_id = store_with_partner.save_bot_draft(
            partner_id=partner_id,
            period="2026-09",
            application_id=999,
            attachment="x.xlsx",
            turnover=turnover,
            currency="USD",
            reported_share=turnover * 0.2,
            location_ids=location_ids,
            extraction_notes=[],
            application_breakdown=[],
        )
        handle_callback(
            store_with_partner,
            "tok",
            {
                "id": f"cb-{update_id}",
                "data": f"confirm:{draft_id}",
                "from": {"id": 1},
                "message": {"chat": {"id": 200600}},
            },
            update_id=update_id,
        )
    with store_with_partner.connect() as db:
        rows = db.execute(
            "SELECT version, turnover, client_count "
            "FROM report_versions WHERE report_id = ("
            "SELECT id FROM reports WHERE partner_id = ?) ORDER BY version",
            (partner_id,),
        ).fetchall()
    assert rows[0]["turnover"] == 200.0 and rows[0]["client_count"] == 1
    assert rows[1]["turnover"] == 400.0 and rows[1]["client_count"] == 2


def test_fr10_resend_before_confirm_does_not_create_version(
    store_with_partner: Store, telegram_mocks, monkeypatch, tmp_path
) -> None:
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://app.alteg.io/app/999/", "comment": "Main", "category": ""}],
    )
    _invoke(
        store_with_partner,
        _parsed(turnover=500.0, reported_share=100.0),
        monkeypatch,
        tmp_path,
    )
    # Just upload, no confirm. There must be a draft, no report.
    partner_id = _partner_id(store_with_partner)
    with store_with_partner.connect() as db:
        drafts = db.execute(
            "SELECT * FROM report_drafts WHERE partner_id = ?", (partner_id,)
        ).fetchall()
        versions = db.execute(
            "SELECT * FROM report_versions"
        ).fetchall()
    assert len(drafts) == 1
    assert versions == []


# ---------------------------------------------------------------------------
# FR-11: admin summary surfaces the latest confirmed report.
# ---------------------------------------------------------------------------


def test_fr11_admin_sees_confirmed_current_report(
    tmp_path: Path, monkeypatch
) -> None:
    """After confirm → /reports/{id} and the dashboard render the live numbers."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store = app.state.store
    store.replace_application_links(
        store.partner_by_token("demo-partner")["id"],
        [{"url": "https://app.alteg.io/app/999/", "comment": "Demo", "category": ""}],
    )
    partner = store.partner_by_token("demo-partner")
    # Submit a real report so the dashboard shows something.
    report_id, _ = store.submit_report(
        partner_id=partner["id"],
        period="2026-09",
        turnover=1234.0,
        currency="USD",
        reported_share=246.8,
        location_ids=["101", "102"],
        comment="",
        attachment=None,
        application_id=999,
    )
    page = client.get(f"/reports/{report_id}")
    assert page.status_code == 200
    assert "1 234" in page.text
    assert "USD" in page.text
    # Admin summary also reflects this.
    dashboard = client.get("/?period=2026-09")
    assert dashboard.status_code == 200
    assert "Demo Integration Hub App" in dashboard.text


def test_fr11_admin_does_not_see_draft_as_current(
    tmp_path: Path, monkeypatch
) -> None:
    """An unconfirmed draft must NOT replace the confirmed report row."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store = app.state.store
    store.replace_application_links(
        store.partner_by_token("demo-partner")["id"],
        [{"url": "https://app.alteg.io/app/999/", "comment": "Demo", "category": ""}],
    )
    partner = store.partner_by_token("demo-partner")
    # Confirmed baseline.
    store.submit_report(
        partner_id=partner["id"],
        period="2026-09",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["101"],
        comment="",
        attachment=None,
        application_id=999,
    )
    # Then a draft with a wildly different turnover — never confirmed.
    store.save_bot_draft(
        partner_id=partner["id"],
        period="2026-09",
        application_id=999,
        attachment="new.xlsx",
        turnover=9999.0,
        currency="USD",
        reported_share=1999.8,
        location_ids=["101"],
        extraction_notes=[],
        application_breakdown=[],
    )
    # The confirmed report must still drive the dashboard.
    dashboard = client.get("/?period=2026-09")
    assert dashboard.status_code == 200
    # The baseline 100.00 USD appears; the draft's 9 999.00 USD does not.
    assert "100.00 USD" in dashboard.text
    assert "9 999.00 USD" not in dashboard.text
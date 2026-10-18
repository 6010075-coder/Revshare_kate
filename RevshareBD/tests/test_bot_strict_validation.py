"""Tests for strict post-parse validation in handle_partner_document.

The bot used to accept "random" files as long as a turnover and at least one
location_id slipped through. We now reject files that miss Turnover/Amount,
Currency, the Application column, locations (none or only one row), or have a
zero turnover — and the rejection message lists everything that is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from revshare.bot import handle_partner_document
from revshare.extract import ParsedPartnerReport
from revshare.store import Store


REJECTION_DESCRIPTIONS: dict[str, str] = {
    "turnover_not_found": "колонки Turnover/Amount",
    "currency_not_found": "колонки Currency",
    "application_not_identified": "ссылки на приложение (Application)",
    "locations_missing": "Location ID (хотя бы одного клиента)",
    "locations_too_few": (
        "достаточного количества строк (нашёл только 1 клиента — отчёт обычно содержит больше)"
    ),
}

REJECTION_FOLLOWUP = (
    "Проверь файл и пришли заново. Если нужна помощь — напиши Kate."
)


def expected_rejection_message(reasons: list[str]) -> str:
    if len(reasons) == 1:
        return f"В файле не нашёл {REJECTION_DESCRIPTIONS[reasons[0]]}. {REJECTION_FOLLOWUP}"
    items = ", ".join(REJECTION_DESCRIPTIONS[r] for r in reasons)
    return f"В файле не хватает: {items}. {REJECTION_FOLLOWUP}"


# ----- Fixtures and helpers (mirror the existing telegram_mocks pattern) -----


@pytest.fixture
def store_with_partner(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="Test App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@test",
        report_channel="telegram",
        token="tkn-1",
        rate=0.2,
        currency="USD",
    )
    store.bind_telegram_chat("tkn-1", "100500")
    # FR-04: a draft can only be saved when the partner has at least one
    # configured application. Seed a default one so the existing
    # rejection-path tests (which only assert the rejection reason, not
    # the application flow) keep running cleanly.
    store.replace_application_links(
        partner_id,
        [
            {
                "url": "https://app.alteg.io/app/999/",
                "comment": "Default app",
                "category": "",
            }
        ],
    )
    return store


@pytest.fixture
def telegram_mocks(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {
        "messages": [],
        "replies": [],
        "callbacks": [],
        "downloaded": b"raw-bytes",
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

    def fake_get_file_path(token, file_id):
        return f"documents/{file_id}.xlsx"

    def fake_download_file(token, file_path):
        return state["downloaded"]

    monkeypatch.setattr("revshare.bot.send_message", fake_send_message)
    monkeypatch.setattr(
        "revshare.telegram.send_message_reply", fake_send_message_reply
    )
    monkeypatch.setattr("revshare.telegram.get_file_path", fake_get_file_path)
    monkeypatch.setattr("revshare.telegram.download_file", fake_download_file)
    return state


def _make_document(file_id: str = "file-1") -> dict:
    return {
        "file_id": file_id,
        "file_name": "report.xlsx",
        "mime_type": "application/octet-stream",
        "file_size": 1024,
    }


def _parsed(**overrides: Any) -> ParsedPartnerReport:
    """Build a ParsedPartnerReport with sensible defaults; override per-test."""
    defaults: dict[str, Any] = dict(
        turnover=1000.0,
        currency="USD",
        reported_share=200.0,
        location_ids=["loc-1", "loc-2"],
        location_source="column",
        notes=[],
        period="2026-08",
        application_breakdown=[
            {"application_id": 1, "location_ids": ["loc-1", "loc-2"]}
        ],
    )
    defaults.update(overrides)
    return ParsedPartnerReport(**defaults)


def _drafts(store: Store) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute("SELECT * FROM report_drafts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _bot_interactions(store: Store) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT kind, partner_id, chat_id, payload, response "
            "FROM bot_interactions WHERE kind = 'document' ORDER BY id"
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


def _spy_save_bot_draft(store: Store) -> dict[str, int]:
    """Wrap store.save_bot_draft with a counter so tests can assert non-call."""
    counter = {"count": 0}
    original = store.save_bot_draft

    def spy(*args: Any, **kwargs: Any) -> Any:
        counter["count"] += 1
        return original(*args, **kwargs)

    store.save_bot_draft = spy  # type: ignore[method-assign]
    return counter


def _invoke(
    store: Store,
    parsed: ParsedPartnerReport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    update_id: int = 100,
) -> None:
    monkeypatch.setattr("revshare.bot.ROOT", tmp_path)
    monkeypatch.setattr(
        "revshare.extract.parse_partner_report",
        lambda *args, **kwargs: parsed,
    )
    handle_partner_document(
        store,
        "tok",
        _make_document(),
        message={"chat": {"id": 100500}, "caption": "2026-08"},
        sender={},
        chat_id=100500,
        update_id=update_id,
    )


# ----- Tests -----


def test_reject_turnover_not_found(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """turnover=None + notes=['turnover_not_found'] → single rejection reason."""
    save_counter = _spy_save_bot_draft(store_with_partner)
    _invoke(
        store_with_partner,
        _parsed(turnover=None, notes=["turnover_not_found"]),
        monkeypatch,
        tmp_path,
    )

    assert save_counter["count"] == 0
    assert len(telegram_mocks["messages"]) == 1
    assert telegram_mocks["messages"][0]["text"] == expected_rejection_message(
        ["turnover_not_found"]
    )
    assert "Turnover/Amount" in telegram_mocks["messages"][0]["text"]
    assert telegram_mocks["replies"] == []
    assert _drafts(store_with_partner) == []

    interactions = _bot_interactions(store_with_partner)
    assert interactions[-1]["response"]["status"] == "rejected"
    assert interactions[-1]["response"]["reasons"] == ["turnover_not_found"]
    assert interactions[-1]["kind"] == "document"
    assert interactions[-1]["partner_id"] is not None


def test_reject_locations_missing(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """turnover found but location_ids=[] → single reason locations_missing."""
    save_counter = _spy_save_bot_draft(store_with_partner)
    _invoke(
        store_with_partner,
        _parsed(
            turnover=500.0,
            location_ids=[],
            notes=[],
            application_breakdown=[],
        ),
        monkeypatch,
        tmp_path,
    )

    assert save_counter["count"] == 0
    assert len(telegram_mocks["messages"]) == 1
    assert "Location ID" in telegram_mocks["messages"][0]["text"]
    assert telegram_mocks["messages"][0]["text"] == expected_rejection_message(
        ["locations_missing"]
    )
    assert _drafts(store_with_partner) == []

    interactions = _bot_interactions(store_with_partner)
    assert interactions[-1]["response"]["status"] == "rejected"
    assert interactions[-1]["response"]["reasons"] == ["locations_missing"]


def test_reject_locations_too_few(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exactly 1 location_id → locations_too_few; message mentions '1 клиента'."""
    save_counter = _spy_save_bot_draft(store_with_partner)
    _invoke(
        store_with_partner,
        _parsed(
            turnover=750.0,
            location_ids=["only-one"],
            notes=[],
            application_breakdown=[
                {"application_id": 1, "location_ids": ["only-one"]}
            ],
        ),
        monkeypatch,
        tmp_path,
    )

    assert save_counter["count"] == 0
    assert len(telegram_mocks["messages"]) == 1
    text = telegram_mocks["messages"][0]["text"]
    assert "1 клиента" in text
    assert text == expected_rejection_message(["locations_too_few"])
    assert _drafts(store_with_partner) == []

    interactions = _bot_interactions(store_with_partner)
    assert interactions[-1]["response"]["status"] == "rejected"
    assert interactions[-1]["response"]["reasons"] == ["locations_too_few"]


def test_accept_zero_turnover_report(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """FR-22/23: turnover=0 with valid locations is a valid report — the partner
    may have clients this month but no payments (e.g. trials). The draft is
    saved and the partner sees a confirmable summary.
    """
    save_counter = _spy_save_bot_draft(store_with_partner)
    _invoke(
        store_with_partner,
        _parsed(
            turnover=0,
            location_ids=["loc-1", "loc-2"],
            notes=[],
            reported_share=0,
        ),
        monkeypatch,
        tmp_path,
    )

    # Draft IS saved — zero turnover is not a rejection reason.
    assert save_counter["count"] == 1
    # No rejection message was sent; the bot instead sent the summary reply.
    assert len(telegram_mocks["messages"]) == 0
    assert len(telegram_mocks["replies"]) == 1
    reply_text = telegram_mocks["replies"][0]["text"]
    assert "Принято" in reply_text
    # Zero figures surface as 0, not None / "value not found".
    assert "Оборот: 0" in reply_text or "Оборот: 0.0" in reply_text
    interactions = _bot_interactions(store_with_partner)
    assert interactions[-1]["response"]["status"] == "draft_saved"


def test_reject_multiple_reasons_at_once(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """turnover=None AND no location_ids → message lists BOTH reasons comma-separated."""
    save_counter = _spy_save_bot_draft(store_with_partner)
    _invoke(
        store_with_partner,
        _parsed(
            turnover=None,
            location_ids=[],
            notes=["turnover_not_found"],
            application_breakdown=[],
        ),
        monkeypatch,
        tmp_path,
    )

    assert save_counter["count"] == 0
    assert len(telegram_mocks["messages"]) == 1
    text = telegram_mocks["messages"][0]["text"]
    assert "не хватает:" in text
    assert "колонки Turnover/Amount" in text
    assert "Location ID" in text
    # Comma-separated, in detection order
    assert "колонки Turnover/Amount, Location ID" in text
    assert text == expected_rejection_message(
        ["turnover_not_found", "locations_missing"]
    )
    assert _drafts(store_with_partner) == []

    interactions = _bot_interactions(store_with_partner)
    response = interactions[-1]["response"]
    assert response["status"] == "rejected"
    assert response["reasons"] == ["turnover_not_found", "locations_missing"]


def test_accept_valid_file_creates_draft(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Valid report → save_bot_draft is called once, 'Принято' summary is sent."""
    save_counter = _spy_save_bot_draft(store_with_partner)
    _invoke(
        store_with_partner,
        _parsed(
            turnover=1000.0,
            currency="USD",
            reported_share=200.0,
            location_ids=["loc-1", "loc-2", "loc-3"],
            notes=[],
            application_breakdown=[
                {"application_id": 7, "location_ids": ["loc-1", "loc-2", "loc-3"]}
            ],
        ),
        monkeypatch,
        tmp_path,
    )

    assert save_counter["count"] == 1
    # Plain send_message was NOT called (rejection path).
    assert telegram_mocks["messages"] == []
    # The summary was sent via send_message_reply with the inline keyboard.
    assert len(telegram_mocks["replies"]) == 1
    reply = telegram_mocks["replies"][0]
    assert reply["chat_id"] == 100500
    assert "Принято" in reply["text"]
    assert "1000.0" in reply["text"]
    assert "USD" in reply["text"]
    assert "Клиентов: 3" in reply["text"]
    keyboard = reply["reply_markup"]["inline_keyboard"][0]
    callback_data = {btn["text"]: btn["callback_data"] for btn in keyboard}
    assert any("confirm" in v for v in callback_data.values())
    assert any("resend" in v for v in callback_data.values())

    drafts = _drafts(store_with_partner)
    assert len(drafts) == 1
    assert drafts[0]["turnover"] == 1000.0
    assert drafts[0]["period"] == "2026-08"

    interactions = _bot_interactions(store_with_partner)
    assert interactions[-1]["response"]["status"] == "draft_saved"
    assert interactions[-1]["response"]["draft_id"] == drafts[0]["id"]
"""Tests for the 'Запросить шаблон' reply-keyboard button and ``/template`` slash command.

These tests cover the new permanent reply keyboard that lives at the bottom of
the partner's Telegram chat. The existing inline ``✅ Подтвердить`` /
``🔁 Переприслать`` keyboard attached to the upload summary stays put —
this feature only adds a persistent bottom keyboard so a partner can ask for a
fresh ``.xlsx`` template any time, without an invite flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from revshare.bot import (
    REQUEST_TEMPLATE_LABEL,
    handle_update,
    send_template_to_partner,
)
from revshare.store import Store
from revshare.telegram import request_template_keyboard


# ----- Fixtures and helpers -----


@pytest.fixture
def store_with_partner(tmp_path: Path) -> Store:
    """A store with a single partner bound to chat 100500 and NO application links."""
    store = Store(tmp_path / "test.db")
    store.add_partner(
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
    return store


def _partner_id(store: Store, chat_id: str = "100500") -> int:
    return int(store.partner_by_chat_id(chat_id)["id"])


@pytest.fixture
def telegram_mocks(monkeypatch: pytest.MonkeyPatch):
    """Patch Telegram API calls for unit tests. Tracks messages, replies, documents, callbacks."""
    state: dict[str, Any] = {
        "messages": [],
        "replies": [],
        "callbacks": [],
        "documents": [],
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

    def fake_answer_callback_query(token, callback_id, text="", show_alert=False):
        state["callbacks"].append(
            {"id": callback_id, "text": text, "show_alert": show_alert}
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
        "revshare.telegram.send_document", fake_send_document
    )
    monkeypatch.setattr(
        "revshare.telegram.answer_callback_query", fake_answer_callback_query
    )
    monkeypatch.setattr("revshare.telegram.get_file_path", fake_get_file_path)
    monkeypatch.setattr("revshare.telegram.download_file", fake_download_file)
    return state


def _bot_interactions(store: Store, kind: str) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT kind, partner_id, chat_id, payload, response, period "
            "FROM bot_interactions WHERE kind = ? ORDER BY id",
            (kind,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "kind": row["kind"],
                "partner_id": row["partner_id"],
                "chat_id": row["chat_id"],
                "period": row["period"],
                "payload": json.loads(row["payload"] or "{}"),
                "response": json.loads(row["response"] or "{}"),
            }
        )
    return out


def _make_document(file_name: str, file_size: int, file_id: str = "file-1") -> dict:
    return {
        "file_id": file_id,
        "file_name": file_name,
        "mime_type": "application/octet-stream",
        "file_size": file_size,
    }


# --- Helper used in fixtures above ---


def _seed_application_link(store: Store, partner_id: int) -> None:
    store.replace_application_links(
        partner_id,
        [
            {
                "url": "https://example.com/app",
                "comment": "main",
                "category": "",
            }
        ],
    )


# Override store_with_partner to also seed an app link.
@pytest.fixture
def store_with_partner_and_apps(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    pid = store.add_partner(
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
    _seed_application_link(store, pid)
    return store


# ----- 1. Send template on button press -----


def test_send_template_on_button_press(
    store_with_partner_and_apps: Store, telegram_mocks
) -> None:
    """Partner taps 'Запросить шаблон' → xlsx sent + confirmation text + logged."""
    partner = store_with_partner_and_apps.partner_by_chat_id("100500")
    result = send_template_to_partner(
        store_with_partner_and_apps,
        partner,
        100500,
        "tok",
        update_id=100,
    )
    assert result["status"] == "sent"
    assert len(telegram_mocks["documents"]) == 1
    doc = telegram_mocks["documents"][0]
    assert doc["chat_id"] == 100500
    assert doc["filename"].startswith("revshare_template_")
    assert doc["filename"].endswith(".xlsx")
    # Confirmation text was sent after the document.
    assert any(
        "Готово" in m["text"] and m["chat_id"] == 100500
        for m in telegram_mocks["messages"]
    )
    interactions = _bot_interactions(
        store_with_partner_and_apps, "template_requested"
    )
    assert len(interactions) == 1
    assert interactions[0]["response"]["status"] == "sent"
    assert interactions[0]["partner_id"] == partner["id"]


# ----- 2. Send template on /template command (with and without bot suffix) -----


@pytest.mark.parametrize(
    "command_text", ["/template", "/template@Altegio_hub_bot"]
)
def test_send_template_on_slash_command(
    store_with_partner_and_apps: Store,
    telegram_mocks,
    command_text: str,
) -> None:
    """``/template`` and ``/template@Altegio_hub_bot`` both send the template."""
    handle_update(
        store_with_partner_and_apps,
        "tok",
        {
            "update_id": 200,
            "message": {
                "chat": {"id": 100500},
                "from": {"id": 1},
                "text": command_text,
            },
        },
    )
    assert len(telegram_mocks["documents"]) == 1
    assert any(
        "Готово" in m["text"] for m in telegram_mocks["messages"]
    )
    interactions = _bot_interactions(
        store_with_partner_and_apps, "template_requested"
    )
    assert len(interactions) == 1
    assert interactions[0]["response"]["status"] == "sent"


# ----- 3. No application links -----


def test_send_template_no_application_links(
    store_with_partner: Store, telegram_mocks
) -> None:
    """Partner with zero application links → friendly text, NO xlsx, status='no_application'."""
    partner = store_with_partner.partner_by_chat_id("100500")
    result = send_template_to_partner(
        store_with_partner, partner, 100500, "tok", update_id=300
    )
    assert result["status"] == "no_application"
    assert telegram_mocks["documents"] == []
    assert any(
        "Сначала админ должен настроить приложения" in m["text"]
        for m in telegram_mocks["messages"]
    )
    interactions = _bot_interactions(store_with_partner, "template_requested")
    assert len(interactions) == 1
    assert interactions[0]["response"]["status"] == "no_application"


# ----- 4. Build error -----


def test_send_template_build_error(
    store_with_partner_and_apps: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_report_template_xlsx raises → fallback text + logged build_error."""
    partner = store_with_partner_and_apps.partner_by_chat_id("100500")

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom-xlsx")

    monkeypatch.setattr(
        "revshare.report_template.build_report_template_xlsx", boom
    )

    result = send_template_to_partner(
        store_with_partner_and_apps,
        partner,
        100500,
        "tok",
        update_id=400,
    )
    assert result["status"] == "build_error"
    assert telegram_mocks["documents"] == []
    assert any(
        "Не получилось собрать шаблон" in m["text"]
        for m in telegram_mocks["messages"]
    )
    interactions = _bot_interactions(
        store_with_partner_and_apps, "template_requested"
    )
    assert len(interactions) == 1
    assert interactions[0]["response"]["status"] == "build_error"


# ----- 5. Unbound chat -----


def test_unbound_chat_text_command(
    tmp_path: Path, telegram_mocks
) -> None:
    """Text from an unbound chat → 'Чат не привязан' text, no template."""
    store = Store(tmp_path / "test.db")
    handle_update(
        store,
        "tok",
        {
            "update_id": 500,
            "message": {
                "chat": {"id": 999999},
                "from": {"id": 1},
                "text": REQUEST_TEMPLATE_LABEL,
            },
        },
    )
    assert telegram_mocks["documents"] == []
    assert any(
        "Чат не привязан" in m["text"] for m in telegram_mocks["messages"]
    )
    # No bot_interactions row written for the unbound case (it's an inbound message
    # that returns immediately without touching the store).
    with store.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM bot_interactions WHERE kind='template_requested'"
        ).fetchone()[0]
    assert count == 0


def test_keyboard_helper_shape() -> None:
    """Sanity-check the keyboard markup that the bot attaches to partner messages."""
    keyboard = request_template_keyboard()
    assert keyboard["keyboard"] == [[{"text": REQUEST_TEMPLATE_LABEL}]]
    assert keyboard["resize_keyboard"] is True
    assert keyboard["one_time_keyboard"] is False


def test_request_template_keyboard_has_no_is_persistent_field() -> None:
    """The reply keyboard markup must NOT carry the non-standard ``is_persistent`` flag.

    Telegram's Bot API has no such field — including it causes the API to
    reject or silently drop the keyboard. Persistence comes from the standard
    ``resize_keyboard: True`` + ``one_time_keyboard: False`` combination.
    """
    keyboard = request_template_keyboard()
    assert "keyboard" in keyboard
    assert "resize_keyboard" in keyboard
    assert "one_time_keyboard" in keyboard
    assert "is_persistent" not in keyboard


# ----- 6. Keyboard attached on success summary -----


def test_keyboard_attached_on_success_summary(
    store_with_partner_and_apps: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Valid file → success summary sent with reply_markup (inline confirm/resend)."""
    from revshare.extract import ParsedPartnerReport

    monkeypatch.setattr("revshare.bot.ROOT", tmp_path)
    monkeypatch.setattr(
        "revshare.extract.parse_partner_report",
        lambda *args, **kwargs: ParsedPartnerReport(
            turnover=1000.0,
            currency="USD",
            reported_share=200.0,
            location_ids=["loc-1", "loc-2"],
            location_source="column",
            notes=[],
        ),
    )
    from revshare.bot import handle_partner_document

    handle_partner_document(
        store_with_partner_and_apps,
        "tok",
        _make_document("data.xlsx", 2048, file_id="abc-success"),
        message={"chat": {"id": 100500}, "caption": "2026-08"},
        sender={},
        chat_id=100500,
        update_id=600,
    )
    replies = telegram_mocks["replies"]
    assert replies, "expected the success summary as a sendMessage reply"
    summary = replies[-1]
    assert summary["reply_markup"] is not None
    kb = summary["reply_markup"].get("inline_keyboard")
    assert kb, "the success summary should keep the inline confirm/resend keyboard"


def test_summary_inline_keyboard_has_template_button(
    store_with_partner_and_apps: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Success summary's inline keyboard has THREE buttons: confirm, resend, template.

    The new `📄 Запросить шаблон` button lets the partner re-request the xlsx
    right after the upload, without waiting for the persistent reply keyboard
    to refresh on the next message.
    """
    import re

    from revshare.extract import ParsedPartnerReport

    monkeypatch.setattr("revshare.bot.ROOT", tmp_path)
    monkeypatch.setattr(
        "revshare.extract.parse_partner_report",
        lambda *args, **kwargs: ParsedPartnerReport(
            turnover=1000.0,
            currency="USD",
            reported_share=200.0,
            location_ids=["loc-1", "loc-2"],
            location_source="column",
            notes=[],
        ),
    )
    from revshare.bot import handle_partner_document

    handle_partner_document(
        store_with_partner_and_apps,
        "tok",
        _make_document("data.xlsx", 2048, file_id="tpl-btn"),
        message={"chat": {"id": 100500}, "caption": "2026-08"},
        sender={},
        chat_id=100500,
        update_id=601,
    )
    summary = telegram_mocks["replies"][-1]
    inline_kb = summary["reply_markup"]["inline_keyboard"]
    # Flatten all buttons across rows.
    all_buttons = [btn for row in inline_kb for btn in row]
    # Three buttons total.
    assert len(all_buttons) == 3
    texts = [b["text"] for b in all_buttons]
    assert any("Подтвердить" in t for t in texts)
    assert any("Переприслать" in t for t in texts)
    template_buttons = [b for b in all_buttons if "Запросить шаблон" in b["text"]]
    assert len(template_buttons) == 1
    # callback_data matches "template:<digits>".
    assert re.fullmatch(r"template:\d+", template_buttons[0]["callback_data"])


# ----- 7. Keyboard attached on rejection -----


def test_keyboard_attached_on_rejection(
    store_with_partner_and_apps: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File rejected (no locations) → rejection text sent WITH reply_markup."""
    from revshare.extract import ParsedPartnerReport

    monkeypatch.setattr(
        "revshare.extract.parse_partner_report",
        lambda *args, **kwargs: ParsedPartnerReport(
            turnover=None,
            currency=None,
            reported_share=None,
            location_ids=[],
            location_source="column",
            notes=["turnover_not_found"],
        ),
    )
    from revshare.bot import handle_partner_document

    handle_partner_document(
        store_with_partner_and_apps,
        "tok",
        _make_document("empty.xlsx", 2048, file_id="reject-1"),
        message={"chat": {"id": 100500}, "caption": "2026-08"},
        sender={},
        chat_id=100500,
        update_id=700,
    )
    rejection = next(
        m
        for m in telegram_mocks["messages"]
        if "В файле не нашёл" in m["text"] or "В файле не хватает" in m["text"]
    )
    assert rejection["reply_markup"] is not None
    assert rejection["reply_markup"]["keyboard"] == [
        [{"text": REQUEST_TEMPLATE_LABEL}]
    ]


# ----- 8. Keyboard NOT attached to unbound chat -----


def test_keyboard_not_attached_to_unbound_chat(
    tmp_path: Path, telegram_mocks
) -> None:
    """Messages to an unbound chat must be sent WITHOUT the persistent reply keyboard."""
    store = Store(tmp_path / "test.db")
    # Send an unbound /start to a chat with no partner — reply should be plain text.
    handle_update(
        store,
        "tok",
        {
            "message": {
                "chat": {"id": 555},
                "from": {"id": 1},
                "text": "/start",
            }
        },
    )
    reply = telegram_mocks["messages"][0]
    assert "Open the personal bot link" in reply["text"]
    assert reply["reply_markup"] is None

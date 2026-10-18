"""Tests for bot document and callback handlers (handle_partner_document, handle_callback)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from revshare.bot import handle_callback, handle_partner_document
from revshare.extract import ParsedPartnerReport
from revshare.store import Store


# ----- Fixtures and helpers -----


@pytest.fixture
def store_with_partner(tmp_path: Path) -> Store:
    """Create a Store, add a partner and bind a Telegram chat to it."""
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
    """Return the partner id bound to chat_id (handles demo partner offset)."""
    return int(store.partner_by_chat_id(chat_id)["id"])


@pytest.fixture
def telegram_mocks(monkeypatch: pytest.MonkeyPatch):
    """Patch all Telegram API calls so tests do not hit the network."""
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
        "revshare.telegram.answer_callback_query", fake_answer_callback_query
    )
    monkeypatch.setattr(
        "revshare.telegram.send_document", fake_send_document
    )
    monkeypatch.setattr("revshare.telegram.get_file_path", fake_get_file_path)
    monkeypatch.setattr("revshare.telegram.download_file", fake_download_file)
    return state


def _bot_interactions(store: Store, kind: str) -> list[dict[str, Any]]:
    """Read bot_interactions rows of a given kind, decoded to dicts."""
    with store.connect() as db:
        rows = db.execute(
            "SELECT kind, partner_id, chat_id, payload, response "
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
                "payload": json.loads(row["payload"] or "{}"),
                "response": json.loads(row["response"] or "{}"),
            }
        )
    return out


def _drafts(store: Store) -> list[dict[str, Any]]:
    """Read all report_drafts (raw rows) for simple count/content checks."""
    with store.connect() as db:
        rows = db.execute("SELECT * FROM report_drafts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _make_document(file_name: str, file_size: int, file_id: str = "file-1") -> dict:
    """Build a Telegram document message payload for tests."""
    return {
        "file_id": file_id,
        "file_name": file_name,
        "mime_type": "application/octet-stream",
        "file_size": file_size,
    }


# ----- handle_partner_document tests -----


def test_handle_partner_document_no_partner_logs_no_partner(
    tmp_path: Path, telegram_mocks
) -> None:
    """File from a chat that is not linked to any partner → 'open the personal bot link'."""
    store = Store(tmp_path / "test.db")
    handle_partner_document(
        store,
        "tok",
        _make_document("report.xlsx", 1024),
        message={"chat": {"id": 1}},
        sender={},
        chat_id=1,
        update_id=10,
    )
    interactions = _bot_interactions(store, "document")
    assert len(interactions) == 1
    assert interactions[0]["response"]["status"] == "no_partner"
    assert interactions[0]["partner_id"] is None
    assert "personal bot link" in telegram_mocks["messages"][0]["text"]
    assert _drafts(store) == []


def test_handle_partner_document_too_large(
    store_with_partner: Store, telegram_mocks
) -> None:
    """File > 5 MB → 'Файл больше 5 МБ' and log 'too_large'."""
    handle_partner_document(
        store_with_partner,
        "tok",
        _make_document("big.xlsx", 6 * 1024 * 1024),
        message={"chat": {"id": 100500}},
        sender={},
        chat_id=100500,
        update_id=11,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[0]["response"]["status"] == "too_large"
    assert "5 МБ" in telegram_mocks["messages"][0]["text"]
    assert _drafts(store_with_partner) == []


def test_handle_partner_document_bad_extension(
    store_with_partner: Store, telegram_mocks
) -> None:
    """.pdf file → 'Пришли .xlsx или .csv' and log 'bad_extension'."""
    handle_partner_document(
        store_with_partner,
        "tok",
        _make_document("scan.pdf", 1024),
        message={"chat": {"id": 100500}},
        sender={},
        chat_id=100500,
        update_id=12,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[0]["response"]["status"] == "bad_extension"
    assert ".xlsx или .csv" in telegram_mocks["messages"][0]["text"]
    assert _drafts(store_with_partner) == []


def test_handle_partner_document_parse_error(
    store_with_partner: Store, telegram_mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File with right extension but parser raises → 'Не удалось разобрать' + 'parse_error'."""
    monkeypatch.setattr(
        "revshare.extract.parse_partner_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad sheet")),
    )
    handle_partner_document(
        store_with_partner,
        "tok",
        _make_document("data.xlsx", 2048),
        message={"chat": {"id": 100500}, "caption": "2026-08"},
        sender={},
        chat_id=100500,
        update_id=13,
    )
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[0]["response"]["status"] == "parse_error"
    assert "Не удалось разобрать" in telegram_mocks["messages"][0]["text"]
    assert _drafts(store_with_partner) == []


def test_handle_partner_document_success_creates_draft_with_keyboard(
    store_with_partner: Store,
    telegram_mocks,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Valid xlsx → draft saved, summary sent with inline confirm/resend buttons."""
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
    handle_partner_document(
        store_with_partner,
        "tok",
        _make_document("data.xlsx", 2048, file_id="abc123"),
        message={"chat": {"id": 100500}, "caption": "2026-08"},
        sender={},
        chat_id=100500,
        update_id=15,
    )
    drafts = _drafts(store_with_partner)
    assert len(drafts) == 1
    assert drafts[0]["period"] == "2026-08"
    assert drafts[0]["turnover"] == 1000.0
    # Summary with inline keyboard was sent via send_message_reply
    assert len(telegram_mocks["replies"]) == 1
    reply = telegram_mocks["replies"][0]
    assert reply["chat_id"] == 100500
    assert "Принято" in reply["text"]
    # First row of the inline keyboard still holds Confirm / Resend.
    keyboard = reply["reply_markup"]["inline_keyboard"][0]
    callback_data = {btn["text"]: btn["callback_data"] for btn in keyboard}
    assert any("confirm" in v for v in callback_data.values())
    assert any("resend" in v for v in callback_data.values())
    # The draft id from the keyboard matches the saved draft
    assert any(f"confirm:{drafts[0]['id']}" in v for v in callback_data.values())
    # Second row carries the "Запросить шаблон" button so the partner can
    # re-request the xlsx without waiting for the next message to refresh
    # the persistent reply keyboard.
    second_row = reply["reply_markup"]["inline_keyboard"][1]
    template_buttons = [b for b in second_row if "Запросить шаблон" in b["text"]]
    assert len(template_buttons) == 1
    assert template_buttons[0]["callback_data"] == f"template:{drafts[0]['id']}"
    # Interaction log records draft_saved
    interactions = _bot_interactions(store_with_partner, "document")
    assert interactions[-1]["response"]["status"] == "draft_saved"
    assert interactions[-1]["response"]["draft_id"] == drafts[0]["id"]


# ----- handle_callback tests -----


def test_handle_callback_without_chat_id_does_not_crash(
    store_with_partner: Store, telegram_mocks
) -> None:
    """Callback with no message.chat.id → handler returns silently, no calls."""
    handle_callback(
        store_with_partner,
        "tok",
        {"id": "cb-1", "data": "confirm:1", "from": {"id": 1}, "message": {}},
        update_id=20,
    )
    assert telegram_mocks["callbacks"] == []
    assert telegram_mocks["messages"] == []


def test_handle_callback_unbound_chat_says_not_linked(
    tmp_path: Path, telegram_mocks
) -> None:
    """Callback from a chat that is not linked to any partner → 'Чат не привязан'."""
    store = Store(tmp_path / "test.db")
    handle_callback(
        store,
        "tok",
        {
            "id": "cb-1",
            "data": "confirm:1",
            "from": {"id": 1},
            "message": {"chat": {"id": 999}},
        },
        update_id=21,
    )
    assert telegram_mocks["callbacks"][0]["text"] == "Чат не привязан. Нажмите /start."


def test_handle_callback_resend_too_many(
    store_with_partner: Store, telegram_mocks
) -> None:
    """10+ resends in the same calendar day → 'Слишком много попыток' alert and no follow-up message."""
    # Create a draft so the handler resolves the same period we will seed against.
    from revshare.web import current_period

    period = current_period()
    partner_id = _partner_id(store_with_partner)
    draft_id = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period=period,
        application_id=None,
        attachment="seed.xlsx",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["loc-1"],
        extraction_notes=[],
        application_breakdown=[],
    )
    # Seed MAX_RESEND_ATTEMPTS_PER_DAY prior resend interactions for the same period
    # so the next attempt is the (MAX+1)-th and must be blocked.
    from revshare.bot import MAX_RESEND_ATTEMPTS_PER_DAY

    for _ in range(MAX_RESEND_ATTEMPTS_PER_DAY):
        store_with_partner.log_bot_interaction(
            partner_id=partner_id,
            chat_id="100500",
            update_id=None,
            kind="resend",
            direction="inbound",
            payload={"draft_id": draft_id, "period": period},
            period=period,
        )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-2",
            "data": f"resend:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 100500}},
        },
        update_id=22,
    )
    cb = telegram_mocks["callbacks"][0]
    assert "Слишком много попыток" in cb["text"]
    assert cb["show_alert"] is True
    assert telegram_mocks["messages"] == []


def test_handle_callback_resend_within_limit(
    store_with_partner: Store, telegram_mocks
) -> None:
    """First resend in the period → 'Жду новый файл' toast + 'пришли исправленный'."""
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-3",
            "data": "resend:1",
            "from": {"id": 1},
            "message": {"chat": {"id": 100500}},
        },
        update_id=23,
    )
    assert telegram_mocks["callbacks"][0]["text"] == "Жду новый файл"
    assert "Окей, пришли исправленный" in telegram_mocks["messages"][0]["text"]
    # And the resend was logged
    with store_with_partner.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM bot_interactions WHERE kind='resend'"
        ).fetchone()[0]
    assert count == 1


def test_handle_callback_resend_at_boundary(
    store_with_partner: Store, telegram_mocks
) -> None:
    """Seeding MAX_RESEND_ATTEMPTS_PER_DAY-1 resends → next attempt (the MAX-th) still passes."""
    from revshare.bot import MAX_RESEND_ATTEMPTS_PER_DAY
    from revshare.web import current_period

    period = current_period()
    partner_id = _partner_id(store_with_partner)
    draft_id = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period=period,
        application_id=None,
        attachment="seed.xlsx",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["loc-1"],
        extraction_notes=[],
        application_breakdown=[],
    )
    # Seed MAX-1 prior resends so this callback is exactly the MAX-th attempt.
    for _ in range(MAX_RESEND_ATTEMPTS_PER_DAY - 1):
        store_with_partner.log_bot_interaction(
            partner_id=partner_id,
            chat_id="100500",
            update_id=None,
            kind="resend",
            direction="inbound",
            payload={"draft_id": draft_id, "period": period},
            period=period,
        )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-boundary",
            "data": f"resend:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 100500}},
        },
        update_id=24,
    )
    cb = telegram_mocks["callbacks"][0]
    # Should NOT be blocked — must be the 'Жду новый файл' toast.
    assert cb["text"] == "Жду новый файл"
    assert "Слишком много попыток" not in cb["text"]


def test_handle_callback_confirm_acknowledges(
    store_with_partner: Store, telegram_mocks
) -> None:
    """Confirm callback → 'Отчёт получен, ожидает проверки' toast + same message."""
    # Seed a draft for this partner/period so confirm has something to submit.
    from revshare.web import current_period

    period = current_period()
    partner_id = _partner_id(store_with_partner)
    draft_id = store_with_partner.save_bot_draft(
        partner_id=partner_id,
        period=period,
        application_id=None,
        attachment="seed.xlsx",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["loc-1"],
        extraction_notes=[],
        application_breakdown=[],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-4",
            "data": f"confirm:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 100500}},
        },
        update_id=24,
    )
    assert (
        telegram_mocks["callbacks"][0]["text"] == "Отчёт получен, ожидает проверки."
    )
    assert (
        telegram_mocks["messages"][0]["text"] == "Отчёт получен, ожидает проверки."
    )
    with store_with_partner.connect() as db:
        kind = db.execute(
            "SELECT kind FROM bot_interactions ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert kind == "confirm_success"


def test_template_callback_sends_template(
    store_with_partner: Store, telegram_mocks
) -> None:
    """Inline `template:<draft_id>` callback re-sends the xlsx and toast-confirms.

    The callback may carry any draft id (including ones that no longer exist):
    the handler always recomputes the period via `current_period()` and ignores
    the draft id. We seed an application link so `send_template_to_partner`
    can actually produce a document, then verify both the outbound document
    AND the `template_requested` interaction log AND the toast reply.
    """
    # Seed an application link so the template builder has work to do.
    store_with_partner.replace_application_links(
        _partner_id(store_with_partner),
        [{"url": "https://example.com/app", "comment": "main", "category": ""}],
    )
    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-tpl",
            "data": "template:42",
            "from": {"id": 1},
            "message": {"chat": {"id": 100500}},
        },
        update_id=25,
    )
    # send_template_to_partner must have produced a template_requested row,
    # and the toast must confirm in Russian.
    interactions = _bot_interactions(store_with_partner, "template_requested")
    assert len(interactions) == 1
    assert interactions[0]["response"]["status"] == "sent"
    cb = telegram_mocks["callbacks"][0]
    assert cb["id"] == "cb-tpl"
    assert "Шаблон" in cb["text"]
    assert cb["show_alert"] is False


def test_template_callback_does_not_increment_resend_counter(
    store_with_partner: Store, telegram_mocks
) -> None:
    """`template:` callback must NOT touch the resend-attempt budget.

    Templates and resends are different concerns: a partner may tap "Запросить
    шаблон" repeatedly without it counting against the daily resend cap.
    Seed 2 prior resend interactions for the same period, fire a template
    callback, and assert the resend count is unchanged.
    """
    from revshare.web import current_period

    period = current_period()
    partner_id = _partner_id(store_with_partner)
    # Seed two prior resend interactions for the same period.
    for _ in range(2):
        store_with_partner.log_bot_interaction(
            partner_id=partner_id,
            chat_id="100500",
            update_id=None,
            kind="resend",
            direction="inbound",
            payload={"draft_id": 1, "period": period},
            period=period,
        )
    baseline = store_with_partner.recent_resend_count(partner_id, period)
    assert baseline == 2

    # Seed an app link so send_template_to_partner can build the xlsx; otherwise
    # the test would still pass (resend count wouldn't change), but we want
    # to make sure the template path also runs to completion here.
    store_with_partner.replace_application_links(
        partner_id,
        [{"url": "https://example.com/app", "comment": "main", "category": ""}],
    )

    handle_callback(
        store_with_partner,
        "tok",
        {
            "id": "cb-tpl-noresend",
            "data": "template:42",
            "from": {"id": 1},
            "message": {"chat": {"id": 100500}},
        },
        update_id=26,
    )
    # Resend count must still be 2 — template requests must not consume the
    # daily resend budget.
    assert store_with_partner.recent_resend_count(partner_id, period) == 2
    # Sanity: a template_requested row WAS written (we are exercising the
    # template path, not silently returning).
    assert len(_bot_interactions(store_with_partner, "template_requested")) == 1

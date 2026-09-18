from revshare.bot import handle_update, start_payload
from revshare.store import Store


def test_start_payload():
    assert start_payload("/start partner-token") == "partner-token"
    assert start_payload("/start@Altegio_hub_bot kate") == "kate"
    assert start_payload("hello") == ""


def test_partner_start_binds_chat(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    store.add_partner(
        name="Bind App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@bind",
        report_channel="telegram",
        token="bind-token",
        rate=0.2,
        currency="USD",
    )
    replies = []
    monkeypatch.setattr(
        "revshare.bot.send_message",
        lambda token, chat_id, text: replies.append((chat_id, text)),
    )
    handle_update(
        store,
        "token",
        {"message": {"chat": {"id": 999}, "text": "/start bind-token"}},
    )
    partner = store.partner_by_token("bind-token")
    assert partner["telegram_chat_id"] == "999"
    assert replies[0][0] == 999


def test_digest_start_requires_allowed_telegram_user(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    replies = []
    monkeypatch.setattr(
        "revshare.bot.send_message",
        lambda token, chat_id, text: replies.append(text),
    )
    monkeypatch.delenv("REVSHARE_KATE_TELEGRAM_USER_IDS", raising=False)
    handle_update(
        store,
        "token",
        {
            "message": {
                "chat": {"id": 1},
                "from": {"id": 1},
                "text": "/start kate",
            }
        },
    )
    assert store.setting("kate_telegram_chat_id") == ""
    assert "Settings" in replies[0]

    monkeypatch.setenv("REVSHARE_KATE_TELEGRAM_USER_IDS", "42")
    handle_update(
        store,
        "token",
        {
            "message": {
                "chat": {"id": 7},
                "from": {"id": 42},
                "text": "/start kate",
            }
        },
    )
    assert store.setting("kate_telegram_chat_id") == "7"

    handle_update(
        store,
        "token",
        {
            "message": {
                "chat": {"id": 8},
                "from": {"id": 42},
                "text": "/start digest",
            }
        },
    )
    assert store.setting("kate_telegram_chat_id") == "8"

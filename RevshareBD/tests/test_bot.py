from revshare.bot import handle_callback, handle_update, start_payload
from revshare.extract import BaseRow
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
        lambda token, chat_id, text, *, reply_markup=None: replies.append(
            (chat_id, text)
        ),
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
        lambda token, chat_id, text, *, reply_markup=None: replies.append(text),
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
    assert "настройках" in replies[0]

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


def _make_bound_partner(store: Store, chat_id: str = "777", token: str = "cb-token"):
    partner_id = store.add_partner(
        name="Confirm App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@confirm",
        report_channel="telegram",
        token=token,
        rate=0.2,
        currency="USD",
    )
    store.bind_telegram_chat(token, chat_id)
    # FR-04: a confirm-bound draft needs an application. Seed one that the
    # tests can reference by its numeric id.
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
    return store.partner_by_chat_id(chat_id), token


def test_bot_confirm_creates_report_and_removes_draft(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    partner, _ = _make_bound_partner(store)

    draft_id = store.save_bot_draft(
        partner_id=partner["id"],
        period="2026-08",
        application_id=999,
        attachment="confirm-app_2026-08_1_report.xlsx",
        turnover=1500.0,
        currency="USD",
        reported_share=300.0,
        location_ids=["101"],
        extraction_notes=[],
        application_breakdown=[],
    )
    store.replace_usage(
        partner_id=partner["id"],
        period="2026-08",
        connected_ids=["101"],
        active_ids=["101"],
    )
    # Step 3: ``_reassess_report`` now reads connected/active from the
    # reconciliation base, not the legacy usage snapshots. Seed an
    # equivalent base so this bot-path test exercises the assessed
    # branch instead of the ``pending_base`` parking branch.
    store.replace_reconciliation_base(
        period="2026-08",
        uploaded_at="2026-09-01T00:00:00+00:00",
        uploaded_by="tests",
        file_path="tests/base.csv",
        rows=[
            BaseRow(1, "101", 0.0, "USD", 1, 1),
        ],
        notes="",
    )

    monkeypatch.setattr(
        "revshare.telegram.answer_callback_query",
        lambda *args, **kwargs: {"ok": True},
    )
    sends: list[tuple[int | str, str]] = []
    monkeypatch.setattr(
        "revshare.bot.send_message",
        lambda token, chat_id, text, *, reply_markup=None: sends.append(
            (chat_id, text)
        ),
    )

    handle_callback(
        store,
        "token",
        {
            "id": "cb-1",
            "data": f"confirm:{draft_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 777}},
        },
        update_id=10,
    )

    assert store.report_draft_by_id(draft_id) is None
    with store.connect() as db:
        row = db.execute(
            "SELECT report_source, attachment, status, risk_level "
            "FROM reports WHERE partner_id = ? AND period = ?",
            (partner["id"], "2026-08"),
        ).fetchone()
    assert row is not None, "expected a report row after confirm"
    assert row["report_source"] == "partner_file_bot"
    assert row["attachment"] == "confirm-app_2026-08_1_report.xlsx"
    assert row["status"] in {"verified", "requires_review"}
    assert row["risk_level"] in {"green", "yellow", "red"}

    with store.connect() as db:
        log_row = db.execute(
            "SELECT * FROM bot_interactions WHERE kind = 'confirm_success' "
            "AND partner_id = ?",
            (partner["id"],),
        ).fetchone()
    assert log_row is not None
    assert log_row["report_id"] is not None
    assert log_row["partner_id"] == partner["id"]

    assert any("Ожидает проверки администратором" in text for _, text in sends)


def test_bot_confirm_rejects_foreign_draft(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    partner, _ = _make_bound_partner(store, chat_id="777")

    other_partner_id = store.add_partner(
        name="Other App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@other",
        report_channel="telegram",
        token="other-token",
        rate=0.2,
        currency="USD",
    )
    foreign_draft = store.save_bot_draft(
        partner_id=other_partner_id,
        period="2026-08",
        application_id=None,
        attachment="other.xlsx",
        turnover=99.0,
        currency="USD",
        reported_share=10.0,
        location_ids=[],
        extraction_notes=[],
        application_breakdown=[],
    )

    alerts: list[str] = []
    monkeypatch.setattr(
        "revshare.telegram.answer_callback_query",
        lambda token, cb_id, text="", show_alert=False: alerts.append(text)
        or {"ok": True},
    )
    monkeypatch.setattr(
        "revshare.bot.send_message",
        lambda *args, **kwargs: None,
    )

    handle_callback(
        store,
        "token",
        {
            "id": "cb-2",
            "data": f"confirm:{foreign_draft}",
            "from": {"id": 1},
            "message": {"chat": {"id": 777}},
        },
        update_id=11,
    )

    # Foreign draft must remain untouched and no report should be created.
    assert store.report_draft_by_id(foreign_draft) is not None
    with store.connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM reports WHERE partner_id = ?",
            (partner["id"],),
        ).fetchone()
    assert row["c"] == 0
    assert alerts and "не найден" in alerts[0]

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from revshare.bot_context import set_current_bot_id, set_current_file_id
from revshare.store import Store
from revshare.web import create_app


def test_bot_registry_keeps_history_but_only_one_bot_active(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    first = store.register_telegram_bot(101, username="old_bot")
    second = store.register_telegram_bot(202, username="new_bot")

    assert store.active_telegram_bot()["id"] == second
    with store.connect() as db:
        rows = db.execute(
            "SELECT telegram_bot_id, is_active FROM telegram_bots ORDER BY id"
        ).fetchall()
    assert [(row["telegram_bot_id"], row["is_active"]) for row in rows] == [
        (101, 0),
        (202, 1),
    ]
    assert first != second


def test_uploaded_report_is_linked_to_file_and_bot(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    bot_id = store.register_telegram_bot(777, username="reports_bot")
    source = tmp_path / "report.csv"
    source.write_text("Location ID,Turnover\n101,100\n", encoding="utf-8")
    file_id = store.register_uploaded_file(
        "stored-report.csv",
        storage_path=source,
        original_name="report.csv",
        mime_type="text/csv",
        source="telegram_bot",
        partner_id=1,
        bot_id=bot_id,
    )
    set_current_bot_id(bot_id)
    set_current_file_id(file_id)
    partner = store.partner_by_token("demo-partner")
    report_id, _ = store.submit_report(
        partner_id=partner["id"],
        period="2026-08",
        turnover=100,
        currency="USD",
        reported_share=30,
        location_ids=["101"],
        comment="",
        attachment="stored-report.csv",
        file_id=file_id,
        source_bot_id=bot_id,
    )

    report = store.report(report_id)
    assert report["file_id"] == file_id
    assert report["source_bot_id"] == bot_id
    assert report["versions"][0]["file_id"] == file_id
    assert report["history"][0]["file_id"] == file_id
    assert report["history"][0]["bot"]["username"] == "reports_bot"
    set_current_bot_id(None)
    set_current_file_id(None)


def test_admin_can_apply_valid_bot_token_without_putting_token_in_db(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "revshare.web.get_me",
        lambda token: {"id": 123456, "username": "new_reports_bot", "first_name": "Reports"},
    )
    monkeypatch.setattr("revshare.web.set_secret", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr("revshare.web.telegram_token", lambda: "new-token")

    response = client.post(
        "/admin/settings/bot", data={"bot_token": "new-token"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert saved == [("TELEGRAM_BOT_TOKEN", "new-token")]
    assert app.state.store.active_telegram_bot()["username"] == "new_reports_bot"
    with app.state.store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM app_settings WHERE value = ?", ("new-token",)
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM telegram_bots WHERE token_fingerprint = ?",
            ("new-token",),
        ).fetchone()[0] == 0
    page = client.get("/admin/settings?tab=bot")
    assert "@new_reports_bot" in page.text
    assert "new-token" not in page.text


def test_invalid_bot_token_leaves_current_configuration_untouched(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("revshare.web.get_me", lambda token: (_ for _ in ()).throw(RuntimeError("bad token")))
    monkeypatch.setattr("revshare.web.set_secret", lambda key, value: calls.append((key, value)))

    response = client.post(
        "/admin/settings/bot", data={"bot_token": "bad-token"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert calls == []
    assert app.state.store.active_telegram_bot() is None


def test_admin_can_remove_active_bot(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "test.db")
    app.state.store.register_telegram_bot(123, username="active_bot")
    client = TestClient(app)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("revshare.web.set_secret", lambda key, value: calls.append((key, value)))

    response = client.post(
        "/admin/settings/bot/remove", follow_redirects=False
    )

    assert response.status_code == 303
    assert calls == [("TELEGRAM_BOT_TOKEN", "")]
    assert app.state.store.active_telegram_bot() is None

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from revshare.dispatch import _without_retired_form_links
from revshare.store import Store
from revshare.timezone import next_notice_send_at
from revshare.web import create_app
from revshare import web, worker


def test_old_channel_migrates_without_losing_email(tmp_path):
    path = tmp_path / "migration.db"
    store = Store(path)
    partner_id = store.add_partner(
        name="Legacy", legal_name="", legal_country="", altegio_legal_entity="",
        contact_name="", email="legacy@example.com", telegram="",
        report_channel="email", token="legacy", rate=None, currency="USD",
    )
    migrated = Store(path).partner(partner_id)
    assert migrated["report_channel"] == "telegram"
    assert migrated["email"] == "legacy@example.com"


def test_web_forms_disabled_by_default_and_test_mode_explicit(tmp_path):
    app = create_app(tmp_path / "default.db")
    client = TestClient(app)
    assert client.get("/submit/demo-partner").status_code == 410
    assert client.post("/submit/demo-partner/parse").status_code == 410
    test_app = create_app(tmp_path / "opt-in.db", public_forms_enabled=True)
    assert TestClient(test_app).get("/submit/demo-partner").status_code == 200


def test_partner_edit_preserves_email_and_archive_is_read_only(tmp_path):
    app = create_app(tmp_path / "edit.db")
    store = app.state.store
    partner_id = store.add_partner(
        name="Legacy", legal_name="", legal_country="", altegio_legal_entity="",
        contact_name="", email="legacy@example.com", telegram="",
        report_channel="email", token="legacy", rate=None, currency="USD",
    )
    client = TestClient(app)
    response = client.post(f"/admin/partners/{partner_id}/edit", data={
        "name": "Edited", "currency": "USD", "email": "overwrite@example.com",
        "report_channel": "email",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert store.partner(partner_id)["email"] == "legacy@example.com"
    assert store.partner(partner_id)["report_channel"] == "telegram"
    assert client.get("/?q=legacy%40example.com").status_code == 200
    assert "Edited" in client.get("/?q=legacy%40example.com").text
    client.post(f"/admin/partners/{partner_id}/archive", follow_redirects=False)
    assert client.get(f"/admin/partners/{partner_id}/edit").status_code == 200
    assert client.post(f"/admin/partners/{partner_id}/edit", data={"name": "No"}).status_code == 404
    assert client.post(f"/admin/partners/{partner_id}/telegram/unlink").status_code == 404


def test_schedule_uses_yerevan_and_skips_today_after_1115():
    before = next_notice_send_at(datetime(2026, 10, 18, 7, 14, tzinfo=timezone.utc))
    after = next_notice_send_at(datetime(2026, 10, 18, 7, 15, tzinfo=timezone.utc))
    assert (before.day, before.hour, before.minute) == (18, 11, 15)
    assert (after.day, after.hour, after.minute) == (19, 11, 15)


def test_old_form_links_are_removed_from_telegram_text_without_changing_other_links():
    text = "Please report: {link} https://example.com/submit/old?period=2026-08\nHelp: https://help.example.com"
    result = _without_retired_form_links(text)
    assert "submit/" not in result
    assert "{link}" not in result
    assert "https://help.example.com" in result


def test_kate_schedule_copy_and_telegram_unlink_placement(tmp_path):
    app = create_app(tmp_path / "ui.db")
    store = app.state.store
    partner = store.partner_by_token("demo-partner")
    store.bind_telegram_chat("demo-partner", "1234")
    client = TestClient(app, cookies={"revshare_language": "ru"})
    settings = client.get("/admin/settings?tab=schedule").text
    assert "в 11:15 по GMT+4" in settings
    assert "каждый час по времени Еревана" not in settings
    assert "отправляет Telegram и почту" not in settings
    page = client.get(f"/admin/partners/{partner['id']}/edit").text
    contact = page.index('id="telegram"')
    unlink = page.index(f'form="telegram-unlink-{partner["id"]}"')
    applications = page.index('class="form-section"', unlink)
    assert contact < unlink < applications
    assert "Отвязать Telegram" in page
    assert f'id="telegram-unlink-{partner["id"]}"' in page
    assert "data-confirm=" in page


@pytest.mark.parametrize("module", [web, worker])
@pytest.mark.parametrize("enabled", ["0", "1"])
def test_scheduled_send_obeys_current_switch(module, enabled, monkeypatch):
    class FakeStore:
        def setting(self, key, default):
            assert key == "notice_scheduler_enabled"
            return enabled

        def record_notice_audit(self, **kwargs):
            pass

    calls = []
    sleeps = 0

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    async def fake_to_thread(function, store):
        calls.append((function, store))

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(module, "seconds_until_notice_send", lambda: 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module._notice_scheduler_loop(FakeStore()))
    assert len(calls) == (1 if enabled == "1" else 0)

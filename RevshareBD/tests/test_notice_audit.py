from datetime import datetime

from fastapi.testclient import TestClient

from revshare.dispatch import send_partner_notice
from revshare.timezone import YEREVAN_TZ
from revshare.web import create_app


def test_notice_audit_keeps_each_attempt_and_reasons(tmp_path):
    app = create_app(tmp_path / "test.db")
    store = app.state.store
    partner = store.partner_by_token("demo-partner")
    fake_sender = lambda chat, text: None
    assert send_partner_notice(store, partner, "2026-08", "invite", sender=fake_sender)["status"] == "blocked"
    store.bind_telegram_chat("demo-partner", "1234")
    partner = store.partner(partner["id"])
    assert send_partner_notice(store, partner, "2026-08", "invite", sender=fake_sender)["status"] == "sent"
    assert send_partner_notice(store, partner, "2026-08", "invite", sender=fake_sender, source="schedule")["status"] == "skipped"
    events = store.notice_audit_entries()
    assert [event["status"] for event in events] == ["skipped", "sent", "blocked"]
    assert events[0]["detail"] == "already_sent"
    assert events[2]["detail"] == "partner_must_start_bot"
    page = TestClient(app, cookies={"revshare_language": "ru"}).get("/admin/settings?tab=notice_log")
    assert page.status_code == 200
    assert "Журнал уведомлений" in page.text
    assert "Telegram не подключён" in page.text
    assert "Уже отправлено" in page.text


def test_scheduler_after_due_time_without_record_is_unconfirmed(tmp_path):
    store = create_app(tmp_path / "test.db").state.store
    now = datetime.now(YEREVAN_TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    store.record_unconfirmed_scheduler_run(now)
    store.record_unconfirmed_scheduler_run(now)
    events = store.notice_audit_entries()
    assert len(events) == 1
    assert events[0]["status"] == "unconfirmed"


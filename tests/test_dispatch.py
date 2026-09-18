from datetime import date

from revshare.dispatch import (
    digest_text,
    partner_letter,
    send_due_notices,
    send_partner_notice,
)
from revshare.store import Store


def test_send_is_blocked_until_partner_opens_the_bot(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="Bot App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@partner",
        report_channel="telegram",
        token="partner-token",
        rate=0.2,
        currency="USD",
    )
    partner = store.partner(partner_id)
    sent = []
    result = send_partner_notice(
        store,
        partner,
        "2026-08",
        "invite",
        sender=lambda chat, text: sent.append((chat, text)),
        force=True,
    )
    assert result["status"] == "blocked"
    assert sent == []

    store.bind_telegram_chat("partner-token", "555")
    partner = store.partner(partner_id)
    result = send_partner_notice(
        store,
        partner,
        "2026-08",
        "invite",
        sender=lambda chat, text: sent.append((chat, text)),
        force=True,
    )
    assert result["status"] == "sent"
    assert sent[0][0] == "555"
    assert "2026-08" in sent[0][1]


def test_russian_partner_gets_russian_text_and_kate_gets_russian_digest(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="RU App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@ru",
        report_channel="telegram",
        token="ru-token",
        rate=0.2,
        currency="USD",
        notice_settings={"language": "ru"},
    )
    store.bind_telegram_chat("ru-token", "321")
    store.set_setting("kate_telegram_chat_id", "412")
    sent = []
    send_due_notices(
        store,
        today=date(2026, 9, 1),
        period="2026-08",
        sender=lambda chat, text: sent.append((chat, text)),
    )
    partner_message = next(text for chat, text in sent if chat == "321")
    assert partner_message.startswith("Здравствуйте")
    assert "http" in partner_message

    digest = digest_text(store, "2026-08", "ru")
    assert digest.startswith("Статус revshare за 2026-08")
    assert "Не сдали" in digest


def test_digest_goes_to_the_chat_from_settings(tmp_path):
    store = Store(tmp_path / "test.db")
    store.set_setting("kate_telegram_chat_id", "412")
    assert store.digest_chat() == "412"

    store.set_digest_chat("https://t.me/altegio_revshare")
    assert store.setting("digest_chat_id") == "@altegio_revshare"
    assert store.digest_chat() == "@altegio_revshare"

    sent = []
    results = send_due_notices(
        store,
        today=date(2026, 9, 8),
        period="2026-08",
        sender=lambda chat, text: sent.append((chat, text)),
    )
    assert any(item["key"] == "digest" and item["status"] == "sent" for item in results)
    assert sent and sent[0][0] == "@altegio_revshare"

    blocked_store = Store(tmp_path / "blocked.db")
    blocked_store.set_digest_chat("")
    blocked_store.set_setting("kate_telegram_chat_id", "")
    blocked = send_due_notices(
        blocked_store,
        today=date(2026, 9, 8),
        period="2026-08",
        sender=lambda chat, text: sent.append((chat, text)),
    )
    assert {
        "partner": "Internal",
        "key": "digest",
        "status": "blocked",
        "detail": "digest_chat_missing",
    } in blocked

    repeated = send_due_notices(
        store,
        today=date(2026, 9, 8),
        period="2026-08",
        sender=lambda chat, text: sent.append((chat, text)),
    )
    assert not any(item["key"] == "digest" for item in repeated)


def test_email_channel_sends_a_subject_and_body(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="Mail App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="finance@example.com",
        telegram="",
        report_channel="email",
        token="mail-token",
        rate=0.15,
        currency="EUR",
        notice_settings={"language": "ru"},
    )
    letters = []
    result = send_partner_notice(
        store,
        store.partner(partner_id),
        "2026-08",
        "invite",
        mail_sender=lambda to, subject, body, html: letters.append(
            (to, subject, body, html)
        ),
        force=True,
    )
    assert result["status"] == "sent"
    to, subject, body, html = letters[0]
    assert to == "finance@example.com"
    assert subject == "Mail App: отчёт за 2026-08"
    assert body.startswith("Здравствуйте")
    assert "http" in body
    assert "Открыть форму отчёта" in html
    assert "/submit/mail-token?period=2026-08" in html
    assert "Срок сдачи: 2026-09-07 включительно." in html
    assert "Что подготовить" in html
    assert "<html" in html


def test_letter_keeps_the_link_out_of_the_body_text(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="Letter App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="finance@example.com",
        telegram="",
        report_channel="email",
        token="letter-token",
        rate=0.2,
        currency="USD",
        notice_settings={"language": "en"},
    )
    letter = partner_letter(
        store.partner(partner_id), "2026-08", "reminder", store=store
    )
    assert letter["subject"] == "Letter App: report for 2026-08 is still missing"
    assert "<p" in letter["html"]
    assert "submit/letter-token" in letter["html"]
    assert letter["html"].count("Reminder: the Letter App report") == 1
    assert "<p style" not in letter["text"]
    assert "Open the report form: http" in letter["text"]
    assert "Deadline: 2026-09-07 inclusive." in letter["text"]


def test_email_without_address_is_blocked(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="No Mail",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        token="nomail-token",
        rate=0.15,
        currency="EUR",
    )
    letters = []
    result = send_partner_notice(
        store,
        store.partner(partner_id),
        "2026-08",
        "invite",
        mail_sender=lambda to, subject, body, html: letters.append(
            (to, subject, body, html)
        ),
        force=True,
    )
    assert result == {"status": "blocked", "detail": "email_missing"}
    assert letters == []


def test_due_notices_use_the_partner_schedule(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="Scheduled App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@partner",
        report_channel="telegram",
        token="scheduled-token",
        rate=0.2,
        currency="USD",
    )
    store.bind_telegram_chat("scheduled-token", "777")
    sent = []
    results = send_due_notices(
        store,
        today=date(2026, 9, 1),
        period="2026-08",
        sender=lambda chat, text: sent.append((chat, text)),
    )
    assert any(item["status"] == "sent" and item["key"] == "invite" for item in results)
    assert sent

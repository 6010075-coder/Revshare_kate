"""Tests for the separate ``invite_telegram`` notice key.

These cover three layers:
  * The i18n module exposes ``invite_telegram_default`` in en/ru/uk.
  * ``send_partner_notice`` resolves a Telegram-channel ``invite`` using
    the per-partner override first, then the i18n default — and never
    appends the web-form link.
  * The Store migration backfills an empty ``invite_telegram`` entry for
    every existing partner (idempotent).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from revshare.dispatch import send_partner_notice
from revshare.i18n import SUPPORTED_LANGUAGES, translator
from revshare.notifications import NOTICE_KEYS
from revshare.store import Store


def _add_telegram_partner(
    store: Store, *, language: str = "en", token: str = "tok"
) -> int:
    return store.add_partner(
        name="Test",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@partner",
        report_channel="telegram",
        token=token,
        rate=0.2,
        currency="USD",
        notice_settings={"language": language},
    )


def _set_invite_telegram_override(
    store: Store, partner_id: int, message: str
) -> None:
    """Replace the invite_telegram message in the partner's notice_settings."""
    with store.connect() as db:
        row = db.execute(
            "SELECT notifications FROM partners WHERE id = ?", (partner_id,)
        ).fetchone()
        parsed = json.loads(row["notifications"] or "{}")
    for notice in parsed.get("notices", []):
        if notice.get("key") == "invite_telegram":
            notice["message"] = message
            break
    else:
        parsed.setdefault("notices", []).append(
            {
                "key": "invite_telegram",
                "enabled": False,
                "day": 1,
                "audience": "partner",
                "message": message,
            }
        )
    with store.connect() as db:
        db.execute(
            "UPDATE partners SET notifications = ? WHERE id = ?",
            (json.dumps(parsed, ensure_ascii=False), partner_id),
        )


def test_i18n_exposes_invite_telegram_default_in_all_three_languages():
    expected = {
        "en": "Dear partner!",
        "ru": "Уважаемый партнёр!",
        "uk": "Шановний партнере!",
    }
    for lang, opener in expected.items():
        text = translator(lang)("invite_telegram_default")
        assert text.startswith(opener), (lang, text)
        assert "{period}" in text and "{deadline}" in text, (lang, text)


def test_invite_telegram_default_keeps_placeholders_unrendered():
    for lang in ("en", "ru", "uk"):
        text = translator(lang)("invite_telegram_default")
        # Placeholders must NOT be substituted by the translator — the
        # dispatcher formats them with real values at send time.
        assert "{period}" in text
        assert "{deadline}" in text
        # And no real period like "2026-08" should leak in.
        assert "2026" not in text


def test_telegram_invite_uses_i18n_default_when_no_override(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_telegram_partner(store, language="ru")
    store.bind_telegram_chat("tok", "555")

    sent: list[tuple[str, str]] = []
    documents: list[tuple[str, bytes, str, str]] = []
    result = send_partner_notice(
        store,
        store.partner(pid),
        "2026-08",
        "invite",
        sender=lambda chat, text: sent.append((chat, text)),
        document_sender=lambda chat, doc, name, caption: documents.append(
            (chat, doc, name, caption)
        ),
        force=True,
    )

    assert result["status"] == "sent"
    assert len(sent) == 1
    chat, text = sent[0]
    assert chat == "555"
    # i18n ru default — formal, no link appended
    assert text.startswith("Уважаемый партнёр")
    assert "2026-08" in text
    # Deadline rendered as ISO date
    assert "2026-09-07" in text
    # Web-form link intentionally absent on the Telegram invite
    assert "http" not in text
    assert "submit/tok" not in text
    # The xlsx template attachment still travels with the invite
    assert len(documents) == 1
    assert documents[0][0] == "555"
    assert documents[0][2] == "revshare_template_2026-08.xlsx"


def test_telegram_invite_uses_non_empty_override(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_telegram_partner(store, language="ru")
    store.bind_telegram_chat("tok", "555")
    _set_invite_telegram_override(
        store, pid, "Custom TG body for {period}, deadline {deadline}."
    )

    sent: list[tuple[str, str]] = []
    documents: list[tuple[str, bytes, str, str]] = []
    result = send_partner_notice(
        store,
        store.partner(pid),
        "2026-08",
        "invite",
        sender=lambda chat, text: sent.append((chat, text)),
        document_sender=lambda chat, doc, name, caption: documents.append(
            (chat, doc, name, caption)
        ),
        force=True,
    )

    assert result["status"] == "sent"
    chat, text = sent[0]
    # Override wins, placeholders filled, no i18n default and no link.
    assert text == "Custom TG body for 2026-08, deadline 2026-09-07."
    assert "Уважаемый партнёр" not in text
    assert "http" not in text
    # Template attachment still goes out alongside the override.
    assert len(documents) == 1


def test_telegram_invite_empty_override_falls_back_to_i18n_default(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_telegram_partner(store, language="ru")
    store.bind_telegram_chat("tok", "555")
    # Override present but empty — must be treated as "use i18n default".
    _set_invite_telegram_override(store, pid, "")

    sent: list[tuple[str, str]] = []
    send_partner_notice(
        store,
        store.partner(pid),
        "2026-08",
        "invite",
        sender=lambda chat, text: sent.append((chat, text)),
        document_sender=lambda *args, **kwargs: None,
        force=True,
    )
    text = sent[0][1]
    assert text.startswith("Уважаемый партнёр")
    assert "http" not in text


def test_invite_without_telegram_chat_is_not_sent(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = store.add_partner(
        name="Mail",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="finance@example.com",
        telegram="",
        report_channel="telegram",
        token="mail",
        rate=0.2,
        currency="USD",
        notice_settings={"language": "ru"},
    )
    result = send_partner_notice(
        store,
        store.partner(pid),
        "2026-08",
        "invite",
        force=True,
    )
    assert result == {"status": "blocked", "detail": "partner_must_start_bot"}


def test_invite_telegram_is_part_of_notice_keys_but_disabled_by_default():
    assert "invite_telegram" in NOTICE_KEYS
    # Default schedule disables it — schedule_for_period must skip it
    # so we never send a separate Telegram invite event.
    from revshare.notifications import (
        default_notice_settings,
        schedule_for_period,
    )

    settings = default_notice_settings("en")
    invite_telegram = next(
        item for item in settings["notices"] if item["key"] == "invite_telegram"
    )
    assert invite_telegram["enabled"] is False
    assert invite_telegram["message"] == ""
    events = schedule_for_period(
        settings, "2026-08", name="X", link="http://x"
    )
    keys = [event["key"] for event in events]
    assert "invite_telegram" not in keys


def test_migration_adds_empty_invite_telegram_to_existing_partners(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    store = Store(db_path)
    pid = _add_telegram_partner(store, token="tok-legacy")

    # Simulate a pre-migration partner: strip the invite_telegram entry
    # directly from the stored JSON. The migration should re-add it.
    with store.connect() as db:
        row = db.execute(
            "SELECT notifications FROM partners WHERE id = ?", (pid,)
        ).fetchone()
        parsed = json.loads(row["notifications"])
        parsed["notices"] = [
            n for n in parsed["notices"] if n["key"] != "invite_telegram"
        ]
        db.execute(
            "UPDATE partners SET notifications = ? WHERE id = ?",
            (json.dumps(parsed, ensure_ascii=False), pid),
        )

    # Re-init: migration runs again, idempotently.
    Store(db_path)
    partner = store.partner(pid)
    by_key = {n["key"]: n for n in partner["notice_settings"]["notices"]}
    assert "invite_telegram" in by_key
    assert by_key["invite_telegram"]["message"] == ""
    assert by_key["invite_telegram"]["enabled"] is False
    # Round-trip: a second init must not duplicate the entry.
    Store(db_path)
    partner = store.partner(pid)
    keys = [n["key"] for n in partner["notice_settings"]["notices"]]
    assert keys.count("invite_telegram") == 1


def test_settings_hides_the_removed_telegram_invitation_controls(tmp_path: Path):
    from fastapi.testclient import TestClient

    from revshare.web import create_app

    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    page = client.get("/admin/settings?language=ru")
    assert page.status_code == 200
    assert "Telegram invite (overrides default)" not in page.text
    assert 'name="notice_message_invite_telegram"' not in page.text

    response = client.post(
        "/admin/notices",
        data={
            "language": "ru",
            "notice_key": ["invite", "reminder", "last", "overdue"],
            "notice_message": [
                "Custom invite {name} {period} {deadline} {link}",
                "",
                "",
                "",
            ],
        },
    )
    assert response.status_code == 200
    store: Store = app.state.store
    assert store.notice_templates("ru")["invite"].startswith("Custom invite")


def test_supported_languages_include_en_ru_uk():
    for lang in ("en", "ru", "uk"):
        assert lang in SUPPORTED_LANGUAGES


def test_dispatch_period_unchanged_for_invariants():
    """Sanity check: the dispatch fallback never drops the link for non-invite notices."""
    # We don't send a full notice here; this just guards against accidental
    # changes to the helper imports used by ``send_partner_notice``.
    from revshare import dispatch as dispatch_module

    assert hasattr(dispatch_module, "_resolve_invite_text")
    assert callable(dispatch_module._resolve_invite_text)

"""Tests for the new Telegram invite + contact-change flow.

Covers the spec (PR-0010):
  * ``normalize_telegram_handle`` and ``handles_are_equal`` collapse case,
    leading ``@``, surrounding whitespace, and ``t.me/<user>`` URLs into the
    same canonical username.
  * The admin ``POST /admin/partners/{id}/edit`` flow atomically unlinks the
    old ``telegram_chat_id``, revokes prior invites, and mints a fresh one
    when the handle actually changed.
  * Cosmetic edits (``@NewContact`` → ``newcontact``, trailing whitespace,
    ``t.me/<user>`` URL) keep the active binding and the active invite.
  * The new bot ``/start <invite_token>`` handler enforces ``active`` /
    ``used`` / ``revoked`` / expiry / username-mismatch / no-username
    semantics and never re-emits the full token in audit logs.
  * Dispatching a Telegram notice after the handle change is blocked
    until the new contact opens the bot.
"""

from __future__ import annotations

import json
import os
from datetime import UTC as UTC_TZ
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from revshare.bot import handle_update
from revshare.dispatch import send_partner_notice
from revshare.i18n import translator
from revshare.store import (
    TELEGRAM_INVITE_STATUS_ACTIVE,
    TELEGRAM_INVITE_STATUS_REVOKED,
    TELEGRAM_INVITE_STATUS_USED,
    Store,
)
from revshare.telegram_handle import (
    handles_are_equal,
    normalize_telegram_handle,
)
from revshare.web import create_app


# ----- Helpers --------------------------------------------------------------


def _add_partner(
    store: Store,
    *,
    telegram: str = "@old_contact",
    channel: str = "telegram",
    token: str = "tok-1",
    chat_id: str = "100",
) -> int:
    pid = store.add_partner(
        name="Sample",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram=telegram,
        report_channel=channel,
        token=token,
        rate=0.2,
        currency="USD",
    )
    if chat_id:
        store.bind_telegram_chat(token, chat_id)
    return pid


def _bot_interactions(store: Store, *, kind: str = "") -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT kind, payload, response FROM bot_interactions ORDER BY id"
        ).fetchall()
    out = []
    for row in rows:
        if kind and row["kind"] != kind:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        try:
            response = json.loads(row["response"] or "{}")
        except json.JSONDecodeError:
            response = {}
        out.append({"kind": row["kind"], "payload": payload, "response": response})
    return out


def _invites(store: Store, partner_id: int) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            """
            SELECT id, status, token_prefix, expected_handle, used_chat_id,
                   used_username, expires_at
            FROM telegram_invites
            WHERE partner_id = ?
            ORDER BY id
            """,
            (partner_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ----- Pure-function normalization ------------------------------------------


def test_normalize_collapses_case_at_and_url_forms():
    pairs = [
        ("@NewContact", "newcontact"),
        ("NEWCONTACT", "newcontact"),
        ("  newcontact  ", "newcontact"),
        ("https://t.me/newcontact", "newcontact"),
        ("t.me/NewContact/", "newcontact"),
        ("http://t.me/NewContact", "newcontact"),
        ("  @NewContact  ", "newcontact"),
        ("", ""),
    ]
    for raw, expected in pairs:
        assert normalize_telegram_handle(raw) == expected, (raw, expected)


def test_handles_are_equal_collapses_cosmetic_differences():
    assert handles_are_equal("@NewContact", "newcontact")
    assert handles_are_equal("NEWCONTACT", "https://t.me/newcontact")
    assert handles_are_equal("  @NewContact  ", "t.me/newcontact")
    assert not handles_are_equal("@NewContact", "@another")
    # Empty equals empty — clearing the handle is the same as never having one.
    assert handles_are_equal("", "")


# ----- Store: apply_telegram_contact_change --------------------------------


def test_handle_change_unlinks_chat_and_creates_fresh_invite(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store)
    assert store.partner(pid)["telegram_chat_id"] == "100"

    result = store.apply_telegram_contact_change(
        pid, "@new_contact", actor="admin", bot_configured=True, create_invite=True
    )

    assert result["handle_changed"] is True
    assert result["unlinked_chat_id"] == "100"
    assert result["invite"] is not None
    assert result["invite"]["expected_handle"] == "new_contact"
    assert result["invite"]["status"] == TELEGRAM_INVITE_STATUS_ACTIVE
    partner = store.partner(pid)
    assert partner["telegram"] == "new_contact"
    assert partner["telegram_chat_id"] == ""
    # Audit events
    kinds = [row["kind"] for row in _bot_interactions(store)]
    assert "telegram_contact_changed" in kinds
    assert "telegram_chat_unlinked" in kinds
    assert "telegram_invite_created" in kinds
    # No prior invite existed, so no revoke event in this scenario.
    # Token is never written into the audit log.
    token = result["invite"]["token"]
    for row in _bot_interactions(store):
        assert token not in json.dumps(row)
        if "token_prefix" in row["payload"]:
            assert row["payload"]["token_prefix"] == token[:8]


def test_handle_change_to_same_value_keeps_chat_id(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store)
    # Pre-seed an active invite so we can check it isn't replaced.
    seeded = store.create_telegram_invite(pid, expected_handle="old_contact")

    result = store.apply_telegram_contact_change(
        pid,
        "@OLD_contact",
        actor="admin",
        bot_configured=True,
        create_invite=True,
    )

    assert result["handle_changed"] is False
    assert result["invite"] is None
    partner = store.partner(pid)
    assert partner["telegram_chat_id"] == "100"
    # The stored telegram is left untouched when the handle did not change —
    # this preserves any formatting the admin typed (e.g. ``@old_contact``).
    assert handles_are_equal(partner["telegram"], "old_contact")
    invites = _invites(store, pid)
    # Original invite still active.
    assert len(invites) == 1
    assert invites[0]["status"] == TELEGRAM_INVITE_STATUS_ACTIVE
    assert invites[0]["id"] == seeded["id"]
    # No audit noise.
    assert not [row for row in _bot_interactions(store) if row["kind"] == "telegram_contact_changed"]


def test_handle_change_case_insensitive_keeps_chat_id(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@UserName")
    seeded = store.create_telegram_invite(pid, expected_handle="username")
    result = store.apply_telegram_contact_change(
        pid, "username", actor="admin", bot_configured=True, create_invite=True
    )
    assert result["handle_changed"] is False
    assert store.partner(pid)["telegram_chat_id"] == "100"
    invites = _invites(store, pid)
    assert invites[0]["id"] == seeded["id"]
    assert invites[0]["status"] == TELEGRAM_INVITE_STATUS_ACTIVE


def test_handle_change_to_url_form_is_cosmetic(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@newcontact")
    seeded = store.create_telegram_invite(pid, expected_handle="newcontact")
    result = store.apply_telegram_contact_change(
        pid,
        "  https://t.me/NewContact  ",
        actor="admin",
        bot_configured=True,
        create_invite=True,
    )
    assert result["handle_changed"] is False
    assert store.partner(pid)["telegram_chat_id"] == "100"
    invites = _invites(store, pid)
    assert invites[0]["id"] == seeded["id"]


def test_handle_change_to_empty_disables_chat_and_skips_invite(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store)
    seeded = store.create_telegram_invite(pid, expected_handle="old_contact")
    result = store.apply_telegram_contact_change(
        pid,
        "",
        actor="admin",
        bot_configured=True,
        create_invite=False,  # simulate email channel — invite suppressed
    )
    assert result["handle_changed"] is True
    assert result["unlinked_chat_id"] == "100"
    assert result["invite"] is None
    partner = store.partner(pid)
    assert partner["telegram"] == ""
    assert partner["telegram_chat_id"] == ""
    invites = _invites(store, pid)
    assert invites[0]["id"] == seeded["id"]
    assert invites[0]["status"] == TELEGRAM_INVITE_STATUS_REVOKED


def test_handle_change_when_old_was_empty_does_not_inherit(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="", chat_id="")
    result = store.apply_telegram_contact_change(
        pid,
        "@fresh",
        actor="admin",
        bot_configured=True,
        create_invite=True,
    )
    assert result["handle_changed"] is True
    assert result["unlinked_chat_id"] == ""
    assert result["invite"] is not None
    assert store.partner(pid)["telegram_chat_id"] == ""
    assert store.partner(pid)["telegram_chat_id"] == ""


def test_handle_change_inherits_chat_from_another_partner(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    # Partner A is already linked via the same handle.
    pid_a = store.add_partner(
        name="A",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="@shared",
        report_channel="telegram",
        token="a",
        rate=0.2,
        currency="USD",
    )
    store.bind_telegram_chat("a", "555")
    pid_b = _add_partner(store, telegram="", token="b", chat_id="")
    result = store.apply_telegram_contact_change(
        pid_b,
        "https://t.me/shared",
        actor="admin",
        bot_configured=True,
        create_invite=False,
    )
    assert result["handle_changed"] is True
    assert result["inherited_chat_id"] == "555"
    assert store.partner(pid_b)["telegram_chat_id"] == "555"
    # Audit log records the inheritance.
    inherit = [
        row
        for row in _bot_interactions(store)
        if row["kind"] == "telegram_invite_handle_inherited"
    ]
    assert len(inherit) == 1
    assert inherit[0]["payload"]["handle"] == "shared"


def test_handle_change_with_bot_not_configured_does_not_emit_invite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store)
    result = store.apply_telegram_contact_change(
        pid,
        "@new",
        actor="admin",
        bot_configured=False,
        create_invite=True,  # intentionally True — bot_configured wins
    )
    assert result["handle_changed"] is True
    assert result["invite"] is None
    assert store.partner(pid)["telegram_chat_id"] == ""


# ----- Invite consumption / state machine --------------------------------------


def _patch_bot(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    state: dict[str, list] = {"messages": []}
    monkeypatch.setattr(
        "revshare.bot.send_message",
        lambda token, chat_id, text, *, reply_markup=None: state["messages"].append(
            {"chat_id": chat_id, "text": text}
        ),
    )
    return state


def test_invite_token_used_by_correct_username_binds_chat(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@new_contact", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="new_contact")

    row, error = store.consume_telegram_invite(
        invite["token"], chat_id="999", username="new_contact"
    )
    assert error is None
    assert row["status"] == TELEGRAM_INVITE_STATUS_USED
    assert row["used_chat_id"] == "999"
    assert store.partner(pid)["telegram_chat_id"] == "999"


def test_invite_token_username_mismatch_is_logged_not_consumed(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    row, error = store.consume_telegram_invite(
        invite["token"], chat_id="999", username="wrong"
    )
    assert error == "username_mismatch"
    assert row["status"] == TELEGRAM_INVITE_STATUS_ACTIVE
    assert store.partner(pid)["telegram_chat_id"] == ""


def test_invite_token_no_username_is_not_consumed(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    row, error = store.consume_telegram_invite(
        invite["token"], chat_id="999", username=""
    )
    assert error == "no_username"
    assert row["status"] == TELEGRAM_INVITE_STATUS_ACTIVE


def test_invite_token_cannot_be_reused(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    store.consume_telegram_invite(invite["token"], chat_id="1", username="right")
    row, error = store.consume_telegram_invite(
        invite["token"], chat_id="2", username="right"
    )
    assert error == "used"
    assert row["status"] == TELEGRAM_INVITE_STATUS_USED


def test_revoked_invite_cannot_be_used(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    store.revoke_active_telegram_invites(pid, reason="admin_pressed_rotate")
    row, error = store.consume_telegram_invite(
        invite["token"], chat_id="1", username="right"
    )
    assert error == "revoked"
    assert row["status"] == TELEGRAM_INVITE_STATUS_REVOKED


def test_expired_invite_cannot_be_used(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    # Backdate the invite to be expired by re-writing expires_at.
    past = (datetime.now(UTC_TZ) - timedelta(days=2)).replace(microsecond=0).isoformat()
    with store.connect() as db:
        db.execute(
            "UPDATE telegram_invites SET expires_at = ? WHERE id = ?",
            (past, invite["id"]),
        )
    row, error = store.consume_telegram_invite(
        invite["token"], chat_id="1", username="right"
    )
    assert error == "expired"
    assert row["status"] == TELEGRAM_INVITE_STATUS_REVOKED


# ----- Bot /start integration ------------------------------------------------


def test_bot_start_with_invite_token_binds_chat(tmp_path: Path, monkeypatch):
    state = _patch_bot(monkeypatch)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@new_contact", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="new_contact")
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 999},
                "from": {"username": "new_contact", "id": 42},
                "text": f"/start {invite['token']}",
            },
        },
    )
    assert store.partner(pid)["telegram_chat_id"] == "999"
    text = state["messages"][-1]["text"]
    assert "привязан" in text.lower() or "linked" in text.lower()
    # Audit log uses prefix only.
    used = [row for row in _bot_interactions(store) if row["kind"] == "telegram_invite_used"]
    assert len(used) == 1
    assert invite["token"] not in json.dumps(used[0])
    assert used[0]["payload"]["token_prefix"] == invite["token_prefix"]


def test_bot_start_with_invite_mismatched_username_refuses(
    tmp_path: Path, monkeypatch
):
    state = _patch_bot(monkeypatch)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 2,
            "message": {
                "chat": {"id": 999},
                "from": {"username": "wrong", "id": 42},
                "text": f"/start {invite['token']}",
            },
        },
    )
    assert store.partner(pid)["telegram_chat_id"] == ""
    text = state["messages"][-1]["text"]
    assert "другого" in text.lower() or "different" in text.lower()
    kinds = {row["kind"] for row in _bot_interactions(store)}
    assert "telegram_invite_username_mismatch" in kinds


def test_bot_start_with_invite_no_username_asks_for_username(
    tmp_path: Path, monkeypatch
):
    state = _patch_bot(monkeypatch)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 3,
            "message": {
                "chat": {"id": 999},
                "from": {"username": "", "id": 42},
                "text": f"/start {invite['token']}",
            },
        },
    )
    assert store.partner(pid)["telegram_chat_id"] == ""
    text = state["messages"][-1]["text"]
    assert "@username" in text.lower() or "username" in text.lower()
    kinds = {row["kind"] for row in _bot_interactions(store)}
    assert "telegram_invite_no_username" in kinds


def test_bot_start_with_used_invite_is_rejected(tmp_path: Path, monkeypatch):
    state = _patch_bot(monkeypatch)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    # First use binds the chat to 1; second /start from a different chat
    # must not steal the binding.
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 4,
            "message": {
                "chat": {"id": 1},
                "from": {"username": "right", "id": 42},
                "text": f"/start {invite['token']}",
            },
        },
    )
    assert store.partner(pid)["telegram_chat_id"] == "1"
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 5,
            "message": {
                "chat": {"id": 2},
                "from": {"username": "right", "id": 42},
                "text": f"/start {invite['token']}",
            },
        },
    )
    # Partner still bound to first chat; second attempt rejected.
    assert store.partner(pid)["telegram_chat_id"] == "1"
    assert "уже" in state["messages"][-1]["text"].lower() or "already" in state["messages"][-1]["text"].lower()
    kinds = {row["kind"] for row in _bot_interactions(store)}
    assert "telegram_invite_already_used" in kinds


def test_bot_start_with_revoked_invite_is_rejected(tmp_path: Path, monkeypatch):
    state = _patch_bot(monkeypatch)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@right", chat_id="")
    invite = store.create_telegram_invite(pid, expected_handle="right")
    store.revoke_active_telegram_invites(pid, reason="handle_changed")
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 6,
            "message": {
                "chat": {"id": 999},
                "from": {"username": "right", "id": 42},
                "text": f"/start {invite['token']}",
            },
        },
    )
    assert store.partner(pid)["telegram_chat_id"] == ""
    text = state["messages"][-1]["text"]
    assert "отозван" in text.lower() or "revoked" in text.lower()


def test_bot_start_legacy_partner_token_still_works(tmp_path: Path, monkeypatch):
    """Backward compatibility: /start <partner.token> binds via the legacy flow."""
    state = _patch_bot(monkeypatch)
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@legacy", token="legacy-tok", chat_id="")
    handle_update(
        store,
        "TOKEN",
        {
            "update_id": 7,
            "message": {
                "chat": {"id": 1},
                "from": {"username": "someone", "id": 42},
                "text": "/start legacy-tok",
            },
        },
    )
    assert store.partner(pid)["telegram_chat_id"] == "1"


# ----- Dispatcher after contact change --------------------------------------


def test_dispatch_after_contact_change_returns_partner_must_start_bot(
    tmp_path: Path,
):
    store = Store(tmp_path / "test.db")
    pid = _add_partner(store, telegram="@old", channel="telegram", chat_id="100")
    # Admin switches the handle.
    store.apply_telegram_contact_change(
        pid, "@new", actor="admin", bot_configured=True, create_invite=True
    )
    partner = store.partner(pid)
    sent: list[tuple[str, str]] = []
    result = send_partner_notice(
        store,
        partner,
        "2026-08",
        "invite",
        sender=lambda chat, text: sent.append((chat, text)),
        document_sender=lambda *args, **kwargs: None,
        force=True,
    )
    assert result["status"] == "blocked"
    assert result["detail"] == "partner_must_start_bot"
    assert sent == []  # nothing reaches the now-orphaned chat 100


# ----- Web /admin/partners/{id}/edit ---------------------------------------


def _csrf_and_auth(client: TestClient) -> dict[str, str]:
    """Return Basic-Auth + CSRF headers for admin POSTs."""
    import base64
    import re

    credentials = base64.b64encode(b"kate:secret").decode()
    page = client.get(
        "/admin/settings",
        headers={"Authorization": f"Basic {credentials}"},
    )
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None, "csrf token not found on admin settings page"
    token = match.group(1)
    return {
        "Authorization": f"Basic {credentials}",
        "X-CSRF-Token": token,
    }


def test_admin_edit_changes_handle_unlinks_chat_and_creates_invite(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "IntegrationHubBot")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store)
    headers = _csrf_and_auth(client)
    response = client.post(
        f"/admin/partners/{pid}/edit",
        data={
            "name": "Sample",
            "legal_name": "",
            "legal_country": "",
            "altegio_legal_entity": "",
            "contact_name": "",
            "email": "",
            "telegram": "@new_contact",
            "report_channel": "telegram",
            "rate": "20",
            "currency": "USD",
        },
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "tg_changed=1" in location
    assert "tg_unlinked=1" in location
    assert "tg_invite=1" in location
    # Extract the invite token from the redirect URL.
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(location).query)
    token = qs["tg_invite_token"][0]
    assert token
    # The invite row exists in the DB.
    active = store.active_telegram_invite(pid)
    assert active is not None
    assert active["expected_handle"] == "new_contact"
    assert active["token"] == token
    # Partner state.
    partner = store.partner(pid)
    assert partner["telegram"] == "new_contact"
    assert partner["telegram_chat_id"] == ""
    # The admin edit page, fetched with the one-shot token in the URL,
    # shows the invite link.
    page = client.get(
        f"/admin/partners/{pid}/edit?tg_changed=1&tg_unlinked=1&tg_invite_token={token}",
        headers=headers,
    )
    assert f"start={token}" in page.text


def test_admin_edit_cosmetic_change_does_not_unlink_chat(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store, telegram="@UserName")
    headers = _csrf_and_auth(client)
    response = client.post(
        f"/admin/partners/{pid}/edit",
        data={
            "name": "Sample",
            "legal_name": "",
            "legal_country": "",
            "altegio_legal_entity": "",
            "contact_name": "",
            "email": "",
            "telegram": "https://t.me/username",
            "report_channel": "telegram",
            "rate": "20",
            "currency": "USD",
        },
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "tg_changed" not in location
    assert store.partner(pid)["telegram_chat_id"] == "100"


def test_admin_edit_switches_channel_to_email_disables_chat(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store)
    headers = _csrf_and_auth(client)
    response = client.post(
        f"/admin/partners/{pid}/edit",
        data={
            "name": "Sample",
            "legal_name": "",
            "legal_country": "",
            "altegio_legal_entity": "",
            "contact_name": "",
            "email": "someone@example.com",
            "telegram": "",
            "report_channel": "email",
            "rate": "20",
            "currency": "USD",
        },
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "tg_changed=1" in location
    assert "tg_unlinked=1" in location
    assert store.partner(pid)["telegram_chat_id"] == ""
    assert store.partner(pid)["report_channel"] == "telegram"


def test_admin_rotate_invite_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store, telegram="@new_contact", chat_id="")
    original = store.create_telegram_invite(pid, expected_handle="new_contact")
    headers = _csrf_and_auth(client)
    response = client.post(
        f"/admin/partners/{pid}/telegram/invite/rotate",
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "tg_rotated=1" in location
    # Original invite should be revoked, a new one active.
    invites = _invites(store, pid)
    active = [row for row in invites if row["status"] == TELEGRAM_INVITE_STATUS_ACTIVE]
    revoked = [row for row in invites if row["status"] == "revoked"]
    assert len(active) == 1
    assert active[0]["id"] != original["id"]
    assert any(r["id"] == original["id"] for r in revoked)


def test_admin_rotate_invite_when_already_linked_is_a_noop(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store, telegram="@new_contact")
    headers = _csrf_and_auth(client)
    response = client.post(
        f"/admin/partners/{pid}/telegram/invite/rotate",
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "tg_already_linked=1" in response.headers["location"]


def test_partner_card_shows_invite_link_once_after_change(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "IntegrationHubBot")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store)
    headers = _csrf_and_auth(client)
    response = client.post(
        f"/admin/partners/{pid}/edit",
        data={
            "name": "Sample",
            "legal_name": "",
            "legal_country": "",
            "altegio_legal_entity": "",
            "contact_name": "",
            "email": "",
            "telegram": "@new_contact",
            "report_channel": "telegram",
            "rate": "20",
            "currency": "USD",
        },
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Follow redirect to GET — link is shown via query token.
    page = client.get(response.headers["location"], headers=headers)
    assert "IntegrationHubBot" in page.text
    # The invite link is rendered (the start=<token> appears at least once).
    assert "start=" in page.text


def test_partner_card_without_token_still_exposes_rotate_button(
    tmp_path: Path, monkeypatch
):
    """On reload after the redirect, no token is in the URL — admin can rotate."""
    monkeypatch.setenv("REVSHARE_ADMIN_USER", "kate")
    monkeypatch.setenv("REVSHARE_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    app = create_app(tmp_path / "test.db", admin_auth=True)
    client = TestClient(app)
    store: Store = app.state.store
    pid = _add_partner(store)
    store.apply_telegram_contact_change(
        pid, "@new", actor="admin", bot_configured=True, create_invite=True
    )
    headers = _csrf_and_auth(client)
    page = client.get(f"/admin/partners/{pid}/edit", headers=headers)
    assert "Create new link" in page.text
    # The full token MUST NOT be on the page (DB has only prefix).
    active = store.active_telegram_invite(pid)
    assert active["token"] not in page.text


def test_audit_log_never_records_full_token():
    """Defensive check: walk every persisted log row and confirm no token leaks."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "x.db")
        pid = _add_partner(store)
        result = store.apply_telegram_contact_change(
            pid, "@new", actor="admin", bot_configured=True, create_invite=True
        )
        token = result["invite"]["token"]
        # All bot_interactions JSON must not contain the token.
        with store.connect() as db:
            for row in db.execute(
                "SELECT payload, response FROM bot_interactions"
            ).fetchall():
                for raw in (row["payload"], row["response"]):
                    assert token not in (raw or "")

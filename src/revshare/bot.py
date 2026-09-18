from __future__ import annotations

import asyncio
import logging
import os

from revshare.secrets import load_secrets, telegram_token
from revshare.store import Store
from revshare.telegram import get_me, get_updates, send_message

log = logging.getLogger("revshare.bot")

DIGEST_START_PAYLOADS = frozenset({"digest", "kate"})


def admin_digest_user_ids() -> set[str]:
    load_secrets()
    raw = os.getenv("REVSHARE_ADMIN_TELEGRAM_USER_IDS", "").strip()
    if not raw:
        raw = os.getenv("REVSHARE_KATE_TELEGRAM_USER_IDS", "").strip()
    return {part.strip() for part in raw.split(",") if part.strip()}


def start_payload(text: str) -> str:
    if not text:
        return ""
    first, *rest = text.split(maxsplit=1)
    command = first.split("@", 1)[0]
    if command != "/start":
        return ""
    return rest[0].strip() if rest else ""


def handle_update(store: Store, token: str, update: dict) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    payload = start_payload(message.get("text") or "")
    if payload.lower() in DIGEST_START_PAYLOADS:
        allowed = admin_digest_user_ids()
        user_id = str(sender.get("id") or "")
        if not allowed or user_id not in allowed:
            send_message(
                token,
                chat_id,
                "Internal summaries are linked in Settings. This chat was not added.",
            )
            return
        store.set_setting("kate_telegram_chat_id", str(chat_id))
        send_message(
            token,
            chat_id,
            "Chat linked. Internal revshare summaries will be sent here.",
        )
        return
    if payload:
        partner = store.bind_telegram_chat(payload, str(chat_id))
        if partner:
            send_message(
                token,
                chat_id,
                f"{partner['name']}: this chat is linked. Report reminders will come here.",
            )
            return
    send_message(
        token,
        chat_id,
        "Open the personal bot link from your Integration Hub manager to connect this chat.",
    )


async def poll_forever(store: Store) -> None:
    token = telegram_token()
    if not token:
        return
    offset_raw = store.setting("telegram_offset")
    offset = int(offset_raw) if offset_raw.isdigit() else None
    try:
        me = await asyncio.to_thread(get_me, token)
        username = me.get("username") or ""
        if username:
            store.set_setting("telegram_bot_username", username)
    except Exception:
        log.exception("Could not read Telegram bot profile")
    while True:
        try:
            updates = await asyncio.to_thread(get_updates, token, offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                await asyncio.to_thread(handle_update, store, token, update)
            if offset is not None:
                store.set_setting("telegram_offset", str(offset))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram polling failed")
            await asyncio.sleep(5)

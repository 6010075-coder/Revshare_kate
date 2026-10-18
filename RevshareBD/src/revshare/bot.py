from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from revshare.secrets import load_secrets, telegram_token
from revshare.store import Store
from revshare.telegram import (
    get_me,
    get_updates,
    request_template_keyboard,
    send_message,
)
from revshare.notifications import notice_language

log = logging.getLogger("revshare.bot")

ROOT = Path(__file__).resolve().parents[2]
UPLOADS = ROOT / "data" / "uploads"

DIGEST_START_PAYLOADS = frozenset({"digest", "kate"})

REQUEST_TEMPLATE_LABEL = "Запросить шаблон"
REQUEST_TEMPLATE_COMMAND = "/template"


# Сообщение партнёру после успешной привязки чата.
# Берём язык из настроек партнёра, фолбэк — notice_language() вернёт "en".
PARTNER_CHAT_LINKED_MESSAGES: dict[str, str] = {
    "en": "{name}: this chat is linked. Report reminders will come here.",
    "uk": "{name}: цей чат прив'язано. Нагадування про звіти надходитимуть сюди.",
    "ru": "{name}: чат привязан. Сюда будут приходить напоминания об отчётах.",
    "pl": "{name}: ten czat jest powiązany. Przypomnienia o raportach będą przychodzić tutaj.",
    "hu": "{name}: ez a csevegés társítva. A jelentés-emlékeztetők ide fognak érkezni.",
    "pt": "{name}: este chat está vinculado. Os lembretes de relatórios chegarão aqui.",
}


def partner_chat_linked_message(partner: dict) -> str:
    """Pick the 'chat linked' text in the partner's language.

    Falls back to English if the partner's language is unset or unsupported.
    """
    language = notice_language(partner.get("language"))
    template = PARTNER_CHAT_LINKED_MESSAGES.get(language) or PARTNER_CHAT_LINKED_MESSAGES["en"]
    return template.format(name=partner["name"])


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


def command_name(text: str) -> str:
    """Return the slash command name from a Telegram message text.

    Example: ``"/template@Altegio_hub_bot"`` → ``"/template"``.
    Returns an empty string if the text does not start with ``/``.
    """
    if not text:
        return ""
    first = text.split(maxsplit=1)[0]
    if not first.startswith("/"):
        return ""
    return first.split("@", 1)[0]


def is_request_template_text(text: str) -> bool:
    """True if the message text is the keyboard button label or the ``/template`` slash command (with or without the bot username suffix)."""
    if not text:
        return False
    stripped = text.strip()
    if stripped == REQUEST_TEMPLATE_LABEL:
        return True
    cmd = command_name(stripped)
    return cmd == REQUEST_TEMPLATE_COMMAND


def handle_update(store: Store, token: str, update: dict) -> None:
    update_id = update.get("update_id")
    callback = update.get("callback_query")
    if callback:
        handle_callback(store, token, callback, update_id)
        return
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    document = message.get("document") or {}
    if document:
        handle_partner_document(
            store, token, document, message, sender, chat_id, update_id
        )
        return
    text = str(message.get("text") or "")
    if text and is_request_template_text(text):
        partner = store.partner_by_chat_id(str(chat_id))
        if not partner:
            send_message(
                token,
                chat_id,
                "Чат не привязан. Нажмите /start и попроси Kate подключить.",
            )
            return
        send_template_to_partner(
            store, partner, chat_id, token, update_id=update_id
        )
        return
    payload = start_payload(text)
    if payload.lower() in DIGEST_START_PAYLOADS:
        allowed = admin_digest_user_ids()
        user_id = str(sender.get("id") or "")
        if not allowed or user_id not in allowed:
            send_message(
                token,
                chat_id,
                "Этот чат не добавлен. Привязка сводки делается в настройках.",
            )
            return
        store.set_setting("kate_telegram_chat_id", str(chat_id))
        send_message(
            token,
            chat_id,
            "Чат привязан. Сюда будут приходить ежемесячные сводки.",
        )
        return
    sender_username = str(sender.get("username") or "").strip()
    if payload:
        # Modern flow: payload is a one-time ``telegram_invites.token``.
        # The classic ``partner.token`` short-link still works as a
        # fallback when the new invite table hasn't been wired up yet
        # (legacy /submit partners) — it follows the historical
        # ``bind_telegram_chat`` path.
        invite = store.find_telegram_invite_by_token(payload)
        if invite is not None:
            _consume_invite_flow(
                store,
                token,
                invite,
                chat_id,
                sender_username,
                update_id=update_id,
            )
            return
        legacy_partner = store.partner_by_token(payload)
        if legacy_partner is not None:
            partner = store.bind_telegram_chat(payload, str(chat_id))
            if partner:
                send_message(
                    token,
                    chat_id,
                    partner_chat_linked_message(partner),
                    reply_markup=request_template_keyboard(),
                )
                return
    if sender_username:
        partner = store.bind_telegram_chat_by_username(
            sender_username, str(chat_id)
        )
        if partner:
            send_message(
                token,
                chat_id,
                partner_chat_linked_message(partner),
                reply_markup=request_template_keyboard(),
            )
            return
    send_message(
        token,
        chat_id,
        "Open the personal bot link from your Integration Hub manager to connect this chat.",
    )


def _consume_invite_flow(
    store: Store,
    token: str,
    invite: dict,
    chat_id: int,
    sender_username: str,
    *,
    update_id: int | None,
) -> None:
    """Apply the modern Telegram-invite flow when a partner opens ``/start <token>``.

    All branches log a ``bot_interactions`` row so an admin can audit why a
    specific chat did (or did not) become linked. The full invite token is
    never written to the journal — only its ``token_prefix``.
    """
    from revshare.telegram_handle import normalize_telegram_handle

    invite_id = invite["id"]
    prefix = invite["token_prefix"]
    invite_row, error = store.consume_telegram_invite(
        invite["token"],
        chat_id=str(chat_id),
        username=sender_username,
    )
    if error == "not_found":
        # Already revoked or never existed. Treat as user-facing error.
        _log_invite_event(
            store,
            invite_id=invite_id,
            prefix=prefix,
            chat_id=chat_id,
            update_id=update_id,
            kind="telegram_invite_not_found",
            payload={"actor": "partner"},
        )
        send_message(
            token,
            chat_id,
            "Эта ссылка больше не активна. Попросите менеджера прислать новую.",
        )
        return
    if error == "used":
        _log_invite_event(
            store,
            invite_id=invite_id,
            prefix=prefix,
            chat_id=chat_id,
            update_id=update_id,
            kind="telegram_invite_already_used",
            payload={
                "actor": "partner",
                "first_used_chat_id": invite_row.get("used_chat_id") if invite_row else "",
            },
        )
        send_message(
            token,
            chat_id,
            "Эта ссылка уже использована. Если это не вы — напишите менеджеру.",
        )
        return
    if error == "revoked":
        _log_invite_event(
            store,
            invite_id=invite_id,
            prefix=prefix,
            chat_id=chat_id,
            update_id=update_id,
            kind="telegram_invite_revoked",
            payload={"actor": "partner", "reason": invite_row.get("revoked_reason") if invite_row else ""},
        )
        send_message(
            token,
            chat_id,
            "Ссылка отозвана. Попросите менеджера прислать новую.",
        )
        return
    if error == "expired":
        _log_invite_event(
            store,
            invite_id=invite_id,
            prefix=prefix,
            chat_id=chat_id,
            update_id=update_id,
            kind="telegram_invite_expired",
            payload={"actor": "partner"},
        )
        send_message(
            token,
            chat_id,
            "Срок ссылки истёк. Попросите менеджера прислать новую.",
        )
        return
    if error == "username_mismatch":
        expected = (invite_row.get("expected_handle") if invite_row else "") or ""
        actual = normalize_telegram_handle(sender_username)
        _log_invite_event(
            store,
            invite_id=invite_id,
            prefix=prefix,
            chat_id=chat_id,
            update_id=update_id,
            kind="telegram_invite_username_mismatch",
            payload={
                "actor": "partner",
                "expected": expected,
                "actual": actual,
            },
        )
        send_message(
            token,
            chat_id,
            "Ссылка предназначена для другого Telegram-контакта. Свяжитесь с менеджером.",
        )
        return
    if error == "no_username":
        _log_invite_event(
            store,
            invite_id=invite_id,
            prefix=prefix,
            chat_id=chat_id,
            update_id=update_id,
            kind="telegram_invite_no_username",
            payload={"actor": "partner", "expected_handle": invite_row.get("expected_handle") if invite_row else ""},
        )
        expected = (invite_row.get("expected_handle") if invite_row else "") or ""
        send_message(
            token,
            chat_id,
            (
                "В вашем Telegram не задан @username — без него нельзя "
                f"подтвердить, что вы {expected or 'тот контакт, которому предназначалась ссылка'}. "
                "Откройте Настройки → Edit Profile и задайте username, затем "
                "нажмите /start ещё раз."
            ),
        )
        return
    # Success — bind chat to partner (and any sibling partners that share
    # the same Telegram handle, matching the original policy).
    partner_id = invite_row["partner_id"]
    expected = invite_row["expected_handle"]
    store.bind_telegram_chat_by_username(expected, str(chat_id))
    # Make sure the invited partner itself has the chat bound even if the
    # username differs slightly in the DB vs the invite (admin may have
    # typed it differently the second time). bind_telegram_chat_by_username
    # matches case-insensitively after stripping ``@``, so this is safe.
    partner = store.partner(partner_id)
    if partner and not partner.get("telegram_chat_id"):
        store.bind_telegram_chat(partner["token"], str(chat_id))
    refreshed = partner or store.partner(partner_id)
    _log_invite_event(
        store,
        invite_id=invite_id,
        prefix=prefix,
        chat_id=chat_id,
        update_id=update_id,
        kind="telegram_invite_used",
        payload={
            "actor": "partner",
            "expected_handle": expected,
            "actual_handle": normalize_telegram_handle(sender_username),
        },
    )
    if refreshed is None:
        return
    send_message(
        token,
        chat_id,
        partner_chat_linked_message(refreshed),
        reply_markup=request_template_keyboard(),
    )


def _log_invite_event(
    store: Store,
    *,
    invite_id: int,
    prefix: str,
    chat_id: int,
    update_id: int | None,
    kind: str,
    payload: dict[str, object],
) -> None:
    """Write a bot_interactions row for an invite flow event.

    The full token never appears here — only the prefix recorded at invite
    creation time. ``partner_id`` is intentionally left None for invite
    branches that haven't been resolved yet (not_found / no_username).
    """
    safe_payload = {"token_prefix": prefix, **payload}
    try:
        store.log_bot_interaction(
            partner_id=None,
            chat_id=str(chat_id),
            update_id=update_id,
            kind=kind,
            direction="inbound",
            payload=safe_payload,
            response={"invite_id": invite_id},
        )
    except Exception:
        log.exception("Failed to log %s interaction", kind)


# ----- Document handler (file from partner) -----


MAX_BOT_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_BOT_EXTENSIONS = {".xlsx", ".csv"}
MAX_RESEND_ATTEMPTS_PER_DAY = 10


# ----- Template request (button + slash command) -----


def send_template_to_partner(
    store: Store,
    partner: dict,
    chat_id: int,
    token: str,
    *,
    update_id: int | None = None,
) -> dict:
    """Send a fresh xlsx report template to the partner for the current period.

    Returns a status dict like {"status": "sent" | "no_application" | "send_error" | "build_error"}.
    Logs each attempt to bot_interactions with kind="template_requested" and the same status.
    """
    from revshare.telegram import send_document

    try:
        from revshare.web import current_period as _current_period
    except Exception:
        _current_period = None

    if _current_period is None:
        period = ""
    else:
        period = _current_period()

    partner_id = partner["id"]
    keyboard = request_template_keyboard()

    def _log(status: str, *, error: str | None = None, extras: dict | None = None) -> None:
        response = {"status": status}
        if error is not None:
            response["error"] = error[:500]
        if extras:
            response.update(extras)
        try:
            store.log_bot_interaction(
                partner_id=partner_id,
                chat_id=str(chat_id),
                update_id=update_id,
                kind="template_requested",
                direction="outbound",
                payload={"period": period},
                response=response,
                period=period,
            )
        except Exception:
            log.exception("Failed to log template_requested interaction")

    try:
        links = store.application_links(partner_id)
    except Exception as exc:
        log.exception("application_links lookup failed")
        _log("build_error", error=str(exc))
        send_message(
            token,
            chat_id,
            "Не получилось собрать шаблон. Напиши Kate, разберёмся.",
            reply_markup=keyboard,
        )
        return {"status": "build_error"}

    if not links:
        _log("no_application")
        send_message(
            token,
            chat_id,
            "Сначала админ должен настроить приложения для этого партнёра.",
            reply_markup=keyboard,
        )
        return {"status": "no_application"}

    try:
        from revshare.report_template import build_report_template_xlsx

        document = build_report_template_xlsx(store, partner, period)
    except Exception as exc:
        log.exception("Failed to build report template xlsx")
        _log("build_error", error=str(exc))
        send_message(
            token,
            chat_id,
            "Не получилось собрать шаблон. Напиши Kate, разберёмся.",
            reply_markup=keyboard,
        )
        return {"status": "build_error"}

    filename = "revshare_template.xlsx"
    caption = "Шаблон для заполнения"
    try:
        send_document(
            token,
            chat_id,
            document,
            filename=filename,
            caption=caption,
        )
    except Exception as exc:
        log.exception("Failed to send template document to chat %s", chat_id)
        _log("send_error", error=str(exc))
        send_message(
            token,
            chat_id,
            "Не получилось собрать шаблон. Напиши Kate, разберёмся.",
            reply_markup=keyboard,
        )
        return {"status": "send_error"}

    send_message(
        token,
        chat_id,
        "Готово — отправляю шаблон. Заполни и пришли файл обратно.",
        reply_markup=keyboard,
    )
    _log("sent", extras={"file_size": len(document)})
    return {"status": "sent"}


def handle_partner_document(
    store: Store,
    token: str,
    document: dict,
    message: dict,
    sender: dict,
    chat_id: int,
    update_id: int | None,
) -> None:
    partner = store.partner_by_chat_id(str(chat_id))
    payload = {
        "file_id": document.get("file_id"),
        "file_name": document.get("file_name"),
        "mime_type": document.get("mime_type"),
        "file_size": document.get("file_size"),
    }
    if not partner:
        store.log_bot_interaction(
            partner_id=None,
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "no_partner"},
        )
        send_message(
            token,
            chat_id,
            "Open the personal bot link from your Integration Hub manager to connect this chat.",
        )
        return

    file_size = int(document.get("file_size") or 0)
    file_name = document.get("file_name") or "report.xlsx"
    keyboard = request_template_keyboard()
    if file_size > MAX_BOT_FILE_BYTES:
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "too_large", "max_bytes": MAX_BOT_FILE_BYTES},
        )
        send_message(
            token,
            chat_id,
            f"Файл больше 5 МБ ({file_size // 1024 // 1024} МБ). Уменьши и пришли заново.",
            reply_markup=keyboard,
        )
        return

    ext = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext not in ALLOWED_BOT_EXTENSIONS:
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "bad_extension"},
        )
        send_message(
            token,
            chat_id,
            "Пришли файл в формате .xlsx или .csv.",
            reply_markup=keyboard,
        )
        return

    from revshare.telegram import (
        download_file as tg_download,
        get_file_path,
    )
    from revshare.extract import parse_partner_report
    from revshare.fx import valid_period
    from revshare.web import current_period

    try:
        file_path = get_file_path(token, document["file_id"])
        raw = tg_download(token, file_path)
    except Exception as exc:  # pragma: no cover - network
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "download_error", "error": str(exc)},
        )
        send_message(token, chat_id, "Не удалось скачать файл. Попробуй ещё раз.")
        return

    caption_period = valid_period(message.get("caption")) or current_period()
    import secrets
    attachment_name = (
        f"{partner['id']}_{caption_period}_{secrets.token_hex(3)}_{file_name}"
    )
    upload_path = ROOT / "data" / "uploads" / attachment_name
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(raw)

    try:
        parsed = parse_partner_report(
            upload_path,
            rate=partner["rate"],
            default_currency=partner["currency"],
            period=caption_period,
        )
    except Exception as exc:  # pragma: no cover - parser edge cases
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "parse_error", "error": str(exc)},
        )
        send_message(
            token,
            chat_id,
            "Не удалось разобрать файл. Проверь, что в нём есть колонки с Location ID, Turnover и Currency. "
            "Попробуй ещё раз — если не получится, я подключу Kate.",
            reply_markup=keyboard,
        )
        return

    # FR-01..08 parser contract: surface hard errors before touching drafts.
    if parsed.error == "rows_with_missing_amount":
        rows_text = ", ".join(str(r) for r in parsed.error_rows)
        message = (
            f"В строках {rows_text} не указана сумма оплаты. "
            "Если оплаты не было, поставьте 0 и пришлите файл повторно."
        )
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={
                "status": "rows_with_missing_amount",
                "error_rows": list(parsed.error_rows),
            },
        )
        send_message(token, chat_id, message, reply_markup=keyboard)
        return
    if parsed.error == "table_structure_not_found":
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "table_structure_not_found"},
        )
        send_message(
            token,
            chat_id,
            "Не удалось разобрать таблицу. Проверь шаблон и пришли заново.",
            reply_markup=keyboard,
        )
        return
    if parsed.error in {"workbook_unreadable", "workbook_has_no_sheets", "empty_first_sheet"}:
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": parsed.error},
        )
        send_message(
            token,
            chat_id,
            "Не удалось прочитать файл. Проверь, что это .xlsx с заполненными листами.",
            reply_markup=keyboard,
        )
        return

    # FR-02: B3 must carry a recognisable period. Without it the draft
    # would carry a NULL period and the partner could not confirm it.
    period = parsed.period
    if not period:
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "period_not_found"},
        )
        send_message(
            token,
            chat_id,
            "Не удалось определить период на первом листе. Проверьте ячейку B3 и пришлите файл заново.",
            reply_markup=keyboard,
        )
        return

    notes = parsed.notes or []
    reasons: list[str] = []
    if "turnover_not_found" in notes:
        reasons.append("turnover_not_found")
    if "currency_not_found" in notes:
        reasons.append("currency_not_found")
    if "application_not_identified" in notes:
        reasons.append("application_not_identified")
    if not parsed.location_ids:
        reasons.append("locations_missing")
    # FR-22: a single client is a valid report (ТЗ acceptance #22). The old
    # `locations_too_few` rule (rejected < 2 clients) was a pre-ТЗ safety net
    # that contradicted the spec; partners may legitimately have only one
    # paying client in a month.
    # FR-22/23: zero-amount reports are valid (partner may have no payments
    # this month). The parser returns turnover=0.0 for both empty reports and
    # reports where every client row has amount=0; both must produce a
    # confirmable draft, not a rejection.
    if reasons:
        descriptions = {
            "turnover_not_found": "колонки Turnover/Amount",
            "currency_not_found": "колонки Currency",
            "application_not_identified": "ссылки на приложение (Application)",
            "locations_missing": "Location ID (хотя бы одного клиента)",
        }
        followup = "Проверь файл и пришли заново. Если нужна помощь — напиши Kate."
        if len(reasons) == 1:
            message = f"В файле не нашёл {descriptions[reasons[0]]}. {followup}"
        else:
            items = ", ".join(descriptions[r] for r in reasons)
            message = f"В файле не хватает: {items}. {followup}"
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "rejected", "reasons": reasons},
        )
        send_message(token, chat_id, message, reply_markup=keyboard)
        return

    # FR-04: one application per partner or one selected. When several
    # partner_applications are configured the partner must pick one via
    # the inline ``select_app:{draft_id}:{app_id}`` keyboard.
    from revshare.altegio_sync import parse_application_id
    from revshare.application_breakdown import KNOWN_APPLICATION_NAMES

    configured_apps: list[dict[str, object]] = []
    for link in store.application_links(partner["id"]):
        app_id = parse_application_id(link.get("url", ""))
        if app_id is None:
            continue
        configured_apps.append(
            {
                "application_id": app_id,
                "url": link.get("url", ""),
                "comment": link.get("comment", "") or "",
                "label": (
                    str(link.get("comment") or "").strip()
                    or KNOWN_APPLICATION_NAMES.get(int(app_id), "")
                    or str(link.get("url") or "")
                ),
            }
        )

    if not configured_apps:
        # FR-04: no configured application blocks the draft entirely.
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={"status": "no_application"},
            period=period,
        )
        send_message(
            token,
            chat_id,
            "У вас нет привязанного приложения. Свяжитесь с Kate (администратором).",
        )
        return

    if len(configured_apps) == 1:
        bound_application_id = int(configured_apps[0]["application_id"])
    else:
        # Selection deferred — save draft with NULL application_id and
        # render a per-application inline keyboard. confirm/resend are not
        # shown until the partner picks one.
        bound_application_id = None

    draft_id = store.save_bot_draft(
        partner_id=partner["id"],
        period=period,
        application_id=bound_application_id,
        attachment=attachment_name,
        turnover=parsed.turnover,
        currency=parsed.currency or partner["currency"],
        reported_share=parsed.reported_share,
        location_ids=parsed.location_ids or parsed.all_location_ids or [],
        extraction_notes=notes,
        application_breakdown=parsed.application_breakdown,
    )

    from revshare.report_template import _format_period
    from revshare.telegram import send_message_reply

    period_human = _format_period(period, "ru")
    clients_count = len(parsed.location_ids or parsed.all_location_ids or [])

    if bound_application_id is None:
        # Multi-app path: render the choice keyboard. The partner picks an
        # app via ``select_app`` and only then sees confirm/resend.
        app_name = ""
        keyboard_rows: list[list[dict[str, str]]] = []
        for app in configured_apps:
            label = str(app["label"])
            keyboard_rows.append(
                [
                    {
                        "text": label,
                        "callback_data": f"select_app:{draft_id}:{app['application_id']}",
                    }
                ]
            )
        summary = (
            f"📊 Принято ({partner['name']}, {period_human})\n"
            f"Приложение: нужно выбрать\n"
            f"Клиентов: {clients_count}\n"
            f"Оборот: {parsed.turnover} {parsed.currency or partner['currency']}\n"
            f"К выплате Altegio: {parsed.reported_share} {parsed.currency or partner['currency']}"
        )
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="document",
            direction="inbound",
            payload=payload,
            response={
                "status": "application_selection_required",
                "draft_id": draft_id,
                "period": period,
                "application_options": [a["application_id"] for a in configured_apps],
            },
            period=period,
            draft_id=draft_id,
        )
        send_message_reply(
            token,
            chat_id,
            summary,
            reply_markup={"inline_keyboard": keyboard_rows},
        )
        return

    app_name = str(
        next(
            (
                a["label"]
                for a in configured_apps
                if a["application_id"] == bound_application_id
            ),
            str(bound_application_id),
        )
    )

    summary = (
        f"📊 Принято ({partner['name']}, {period_human})\n"
        f"Приложение: {app_name}\n"
        f"Клиентов: {clients_count}\n"
        f"Оборот: {parsed.turnover} {parsed.currency or partner['currency']}\n"
        f"К выплате Altegio: {parsed.reported_share} {parsed.currency or partner['currency']}"
    )

    store.log_bot_interaction(
        partner_id=partner["id"],
        chat_id=str(chat_id),
        update_id=update_id,
        kind="document",
        direction="inbound",
        payload=payload,
        response={
            "status": "draft_saved",
            "draft_id": draft_id,
            "period": period,
            "turnover": parsed.turnover,
            "currency": parsed.currency,
            "client_count": clients_count,
            "application_id": bound_application_id,
        },
        period=period,
        draft_id=draft_id,
    )

    confirm_keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Подтвердить",
                    "callback_data": f"confirm:{draft_id}",
                },
                {
                    "text": "🔁 Переприслать",
                    "callback_data": f"resend:{draft_id}",
                },
            ],
            [
                {
                    "text": "📄 Запросить шаблон",
                    "callback_data": f"template:{draft_id}",
                },
            ],
        ]
    }
    send_message_reply(token, chat_id, summary, reply_markup=confirm_keyboard)


def _application_label_for(
    store: Store, partner_id: int, application_id: int
) -> str:
    """Best-effort label for an application_id — comment, KNOWN name, or url.

    Used by ``handle_callback`` (select_app / confirm summary) and web
    flows so a partner can recognise the chosen application.
    """
    from revshare.altegio_sync import parse_application_id
    from revshare.application_breakdown import KNOWN_APPLICATION_NAMES

    for link in store.application_links(partner_id):
        parsed_id = parse_application_id(link.get("url", ""))
        if parsed_id == application_id:
            comment = str(link.get("comment") or "").strip()
            if comment:
                return comment
            if application_id in KNOWN_APPLICATION_NAMES:
                return KNOWN_APPLICATION_NAMES[application_id]
            return str(link.get("url") or "")
    return str(application_id)


def _draft_summary_lines(
    partner: dict,
    draft: dict,
    *,
    period: str,
    application_label: str,
    currency: str,
) -> str:
    """Compose the partner-facing draft summary text."""
    from revshare.report_template import _format_period

    period_human = _format_period(period, "ru") or "не определён"
    turnover = draft.get("turnover")
    reported_share = draft.get("reported_share")
    clients_count = len(draft.get("location_ids") or [])
    return (
        f"📊 Принято ({partner['name']}, {period_human})\n"
        f"Приложение: {application_label}\n"
        f"Клиентов: {clients_count}\n"
        f"Оборот: {turnover} {currency}\n"
        f"К выплате Altegio: {reported_share} {currency}"
    )


# ----- Callback query handler (inline buttons) -----


def handle_callback(
    store: Store, token: str, callback: dict, update_id: int | None
) -> None:
    from revshare.telegram import (
        answer_callback_query,
        edit_message_text,
        send_message_reply,
    )

    callback_id = callback.get("id")
    data = callback.get("data") or ""
    sender = callback.get("from") or {}
    chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
    message_id = (callback.get("message") or {}).get("message_id")
    if not callback_id or not chat_id:
        return

    partner = store.partner_by_chat_id(str(chat_id))
    if not partner:
        answer_callback_query(token, callback_id, "Чат не привязан. Нажмите /start.")
        return

    if ":" not in data:
        answer_callback_query(token, callback_id)
        return
    action, _, rest = data.partition(":")
    # ``select_app:<draft_id>:<app_id>`` carries two ints after the action
    # prefix; other actions carry only the draft id.
    if action == "select_app":
        raw_draft_id, _, raw_app_id = rest.partition(":")
        try:
            draft_id = int(raw_draft_id)
            app_id = int(raw_app_id)
        except ValueError:
            answer_callback_query(token, callback_id)
            return
    else:
        try:
            draft_id = int(rest)
        except ValueError:
            answer_callback_query(token, callback_id)
            return

    if action == "select_app":
        # FR-04: bind the chosen application to the draft, then surface the
        # full summary (with confirm/resend buttons) to the partner.
        from revshare.altegio_sync import parse_application_id

        draft = store.report_draft_by_id(draft_id)
        if not draft or int(draft.get("partner_id") or 0) != int(partner["id"]):
            store.log_bot_interaction(
                partner_id=partner["id"],
                chat_id=str(chat_id),
                update_id=update_id,
                kind="select_app",
                direction="inbound",
                payload={"draft_id": draft_id, "app_id": app_id},
                response={"status": "draft_not_found"},
                draft_id=draft_id,
            )
            answer_callback_query(
                token,
                callback_id,
                "Черновик не найден. Пришли файл заново.",
                show_alert=True,
            )
            return
        owned_ids = {
            parse_application_id(link.get("url", ""))
            for link in store.application_links(partner["id"])
        }
        if app_id not in owned_ids:
            store.log_bot_interaction(
                partner_id=partner["id"],
                chat_id=str(chat_id),
                update_id=update_id,
                kind="select_app",
                direction="inbound",
                payload={"draft_id": draft_id, "app_id": app_id},
                response={"status": "application_not_owned"},
                draft_id=draft_id,
            )
            answer_callback_query(
                token,
                callback_id,
                "Это приложение к вам не привязано.",
                show_alert=True,
            )
            return
        store.set_draft_application(draft_id, partner["id"], app_id)
        refreshed = store.report_draft_by_id(draft_id) or draft
        app_label = _application_label_for(store, partner["id"], app_id)
        currency = refreshed.get("currency") or partner["currency"]
        summary = _draft_summary_lines(
            partner,
            refreshed,
            period=refreshed["period"],
            application_label=app_label,
            currency=currency,
        )
        confirm_keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Подтвердить",
                        "callback_data": f"confirm:{draft_id}",
                    },
                    {
                        "text": "🔁 Переприслать",
                        "callback_data": f"resend:{draft_id}",
                    },
                ],
                [
                    {
                        "text": "📄 Запросить шаблон",
                        "callback_data": f"template:{draft_id}",
                    },
                ],
            ]
        }
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="select_app",
            direction="inbound",
            payload={"draft_id": draft_id, "app_id": app_id},
            response={"status": "bound", "application_label": app_label},
            draft_id=draft_id,
            period=refreshed.get("period"),
        )
        answer_callback_query(token, callback_id, "Приложение выбрано")
        if message_id is not None:
            try:
                edit_message_text(
                    token,
                    chat_id,
                    message_id,
                    summary,
                    reply_markup=confirm_keyboard,
                )
                return
            except Exception:
                log.exception("edit_message_text failed, falling back to reply")
        send_message_reply(
            token,
            chat_id,
            summary,
            reply_markup=confirm_keyboard,
        )
        return

    if action == "resend":
        from revshare.web import current_period
        from revshare.fx import valid_period

        draft = store.report_draft_by_id(draft_id)
        period = (
            draft.get("period") if draft else None
        ) or valid_period(None) or current_period()
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="resend",
            direction="inbound",
            payload={"draft_id": draft_id, "period": period},
            period=period,
        )
        resend_count = store.recent_resend_count(partner["id"], period)
        if resend_count > MAX_RESEND_ATTEMPTS_PER_DAY:
            answer_callback_query(
                token,
                callback_id,
                "Слишком много попыток. Я подключу Kate.",
                show_alert=True,
            )
            return
        answer_callback_query(token, callback_id, "Жду новый файл")
        send_message(
            token,
            chat_id,
            "Окей, пришли исправленный файл .xlsx или .csv.",
        )
        return

    if action == "template":
        from revshare.web import current_period

        # Partner tapped the inline "📄 Запросить шаблон" button attached to the
        # upload summary. Send a fresh xlsx for the current period — the draft
        # id is unused, we always compute `period` from `current_period()` so
        # this remains robust after drafts are deleted or the callback_data is
        # empty. Crucially, we do NOT touch `recent_resend_count` — template
        # requests are not resends and must not consume the daily budget.
        raw_draft_id = rest
        period = current_period()
        send_template_to_partner(
            store, partner, chat_id, token, update_id=update_id
        )
        answer_callback_query(
            token,
            callback_id,
            "Шаблон отправляю в чат.",
            show_alert=False,
        )
        return

    if action == "confirm":
        draft = store.report_draft_by_id(draft_id)
        if not draft or int(draft.get("partner_id") or 0) != int(partner["id"]):
            store.log_bot_interaction(
                partner_id=partner["id"],
                chat_id=str(chat_id),
                update_id=update_id,
                kind="confirm",
                direction="inbound",
                payload={"draft_id": draft_id, "found": bool(draft)},
                response={"status": "draft_not_found"},
                draft_id=draft_id,
            )
            answer_callback_query(
                token,
                callback_id,
                "Черновик не найден или уже обработан. Пришли файл заново.",
                show_alert=True,
            )
            return
        # FR-09: confirm must be idempotent. If the draft was already
        # deleted (double-click, race) we tell the partner to resend the
        # file instead of writing a second report version.
        if draft.get("application_id") is None:
            store.log_bot_interaction(
                partner_id=partner["id"],
                chat_id=str(chat_id),
                update_id=update_id,
                kind="confirm",
                direction="inbound",
                payload={"draft_id": draft_id},
                response={"status": "application_required"},
                draft_id=draft_id,
                period=draft.get("period"),
            )
            answer_callback_query(
                token,
                callback_id,
                "Сначала выберите приложение.",
                show_alert=True,
            )
            return
        try:
            report_id, _review = store.submit_report(
                partner_id=partner["id"],
                period=draft["period"],
                turnover=float(draft.get("turnover") or 0),
                currency=draft.get("currency") or "USD",
                reported_share=draft.get("reported_share"),
                location_ids=draft.get("location_ids") or [],
                comment="",
                attachment=draft.get("attachment"),
                claims=[],
                application_breakdown=draft.get("application_breakdown") or [],
                report_source="partner_file_bot",
                application_id=draft.get("application_id"),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Failed to submit report from bot draft %s", draft_id)
            store.log_bot_interaction(
                partner_id=partner["id"],
                chat_id=str(chat_id),
                update_id=update_id,
                kind="confirm_error",
                direction="inbound",
                payload={"draft_id": draft_id, "error": str(exc)},
                response={"status": "submit_error"},
                draft_id=draft_id,
                period=draft.get("period"),
            )
            answer_callback_query(
                token,
                callback_id,
                "Не удалось сохранить отчёт. Попробуй ещё раз.",
                show_alert=True,
            )
            return
        # FR-09: delete the draft only after the report+version landed.
        # If the DELETE itself fails the next confirm will still find the
        # draft and report the duplicate, so the partner is protected from
        # double submissions while keeping the report intact.
        try:
            store.delete_report_draft(draft["token"], partner["id"])
        except Exception:
            log.exception(
                "Failed to delete draft %s after successful submit", draft_id
            )
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=update_id,
            kind="confirm_success",
            direction="outbound",
            payload={"draft_id": draft_id, "period": draft.get("period")},
            response={"status": "submitted", "report_id": report_id},
            report_id=report_id,
            draft_id=draft_id,
            period=draft.get("period"),
        )
        answer_callback_query(
            token,
            callback_id,
            "Отчёт подтверждён. Ожидает проверки администратором.",
        )
        send_message(
            token,
            chat_id,
            "Отчёт подтверждён. Ожидает проверки администратором.",
        )
        return

    answer_callback_query(token, callback_id)


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

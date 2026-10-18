from __future__ import annotations

from datetime import date
from typing import Any, Callable

from revshare.email_letter import build_letter
from revshare.i18n import translator
from revshare.notifications import (
    deadline_date,
    format_notice,
    reporting_period,
    schedule_for_period,
)
from revshare.report_template import build_report_template_xlsx
from revshare.secrets import telegram_token
from revshare.timezone import local_today
from revshare.store import Store
from revshare.telegram import TelegramError, send_document, send_message


Sender = Callable[[str, str], None]
DocumentSender = Callable[[str, bytes, str, str], None]

def bot_start_link(bot_username: str, start: str) -> str:
    return f"https://t.me/{bot_username}?start={start}"


def due_events(
    partner: dict, today: date, period: str, store: Store | None = None
) -> list[dict[str, Any]]:
    return [
        event
        for event in schedule_for_period(
            partner["notice_settings"],
            period,
            name=partner["name"],
            link="",
        )
        if event["send_on"] == today
    ]


def should_skip_because_submitted(store: Store, partner_id: int, period: str, key: str) -> bool:
    if key == "invite":
        return False
    if key == "digest":
        return False
    return store.report_id_for(partner_id, period) is not None


DIGEST_LABELS = {
    "ru": {
        "title": "Статус revshare за {period}",
        "submitted": "Сдали: {count}",
        "missing": "Не сдали: {count}",
        "review": "Требуют проверки: {count}",
        "missing_list": "Не сдали: {names}",
        "review_list": "Проверить: {names}",
        "unexplained": "Без объяснений от партнёра: {names}",
    },
    "en": {
        "title": "Revshare status for {period}",
        "submitted": "Submitted: {count}",
        "missing": "Not submitted: {count}",
        "review": "Need review: {count}",
        "missing_list": "Not submitted: {names}",
        "review_list": "Review: {names}",
        "unexplained": "No partner explanation: {names}",
    },
}


def digest_text(store: Store, period: str, language: str = "ru") -> str:
    labels = DIGEST_LABELS.get(language, DIGEST_LABELS["ru"])
    items = store.dashboard(period)
    submitted = [item for item in items if item["report_id"]]
    missing = [item for item in items if not item["report_id"]]
    review = [item for item in submitted if item["status"] == "requires_review"]
    unexplained = []
    for item in submitted:
        report = store.report(item["report_id"])
        if any(claim["reason"] == "unexplained" for claim in report["claims"]):
            unexplained.append(item)
    lines = [
        labels["title"].format(period=period),
        labels["submitted"].format(count=len(submitted)),
        labels["missing"].format(count=len(missing)),
        labels["review"].format(count=len(review)),
    ]
    if missing:
        lines.append(
            labels["missing_list"].format(
                names=", ".join(item["name"] for item in missing)
            )
        )
    if review:
        lines.append(
            labels["review_list"].format(
                names=", ".join(item["name"] for item in review)
            )
        )
    if unexplained:
        lines.append(
            labels["unexplained"].format(
                names=", ".join(item["name"] for item in unexplained)
            )
        )
    return "\n".join(lines)


def partner_letter(
    partner: dict,
    period: str,
    notice_key: str,
    *,
    message: str | None = None,
    store: Store | None = None,
) -> dict[str, str]:
    settings = partner["notice_settings"]
    link = ""
    deadline = deadline_date(settings, period)
    if message is None:
        template = next(
            (
                notice["message"]
                for notice in settings["notices"]
                if notice["key"] == notice_key
            ),
            "",
        )
        message = format_notice(
            template,
            name=partner["name"],
            period=period,
            deadline=deadline.isoformat(),
            link=link,
        )
    return build_letter(
        notice_key,
        settings["language"],
        name=partner["name"],
        period=period,
        deadline=deadline.isoformat(),
        link=link,
        message=message,
    )


def _resolve_invite_text(partner: dict, period: str, deadline_iso: str) -> str:
    """Pick the text used for a Telegram-channel ``invite`` notice.

    Resolution order:
      1. Per-partner override stored in
         ``partner['notice_settings']['notices']`` under
         ``key == 'invite_telegram'``. Used when present and non-empty
         (the entry may contain ``{name}``, ``{period}`` and ``{deadline}``
         placeholders — they are filled via ``format_notice``).
      2. i18n default ``invite_telegram_default`` (translated with the
         partner's language, falling back to ``en``). The web-form link is
         intentionally NOT appended here because Telegram invites always
         travel with the xlsx template attachment.

    The function never returns the empty string; callers can rely on a
    non-empty message when the function returns.
    """
    settings = partner.get("notice_settings") or {}
    notices = settings.get("notices") or []
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        if notice.get("key") != "invite_telegram":
            continue
        override = str(notice.get("message") or "").strip()
        if override:
            return format_notice(
                override,
                name=partner.get("name", ""),
                period=period,
                deadline=deadline_iso,
            )
    language = str(settings.get("language") or "en")
    t = translator(language)
    return t("invite_telegram_default", period=period, deadline=deadline_iso)


def attach_invite_template(
    store: Store,
    partner: dict,
    period: str,
    chat_id: str,
    bot_token: str | None,
    document_sender: DocumentSender | None,
) -> None:
    """Attach the report template to the first partner invitation.

    Only invoked when ``notice_key == 'invite'`` and the partner is on the
    Telegram channel. Email notifications never carry the template; partners
    receive the form URL and can download the template themselves.

    Failures here are logged but never block the notification itself — the
    text message has already been delivered and a missing attachment is a
    softer UX problem than a missing reminder.
    """
    filename = f"revshare_template_{period}.xlsx"
    caption = "Шаблон для заполнения"
    token = bot_token or telegram_token()
    if not document_sender and not token:
        # No way to deliver the attachment (e.g. in unit tests without a
        # bot token). Log a skip and return — the text notice itself is
        # already on its way via the message sender.
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=None,
            kind="template_sent",
            direction="outbound",
            payload={"period": period, "filename": filename},
            response={"status": "skipped", "reason": "no_bot_token"},
            period=period,
        )
        return
    try:
        notice_settings = partner.get("notice_settings") or {}
        language = str(notice_settings.get("language") or "ru")
        document = build_report_template_xlsx(store, partner, period, language=language)
    except Exception as error:  # noqa: BLE001 - logged and swallowed
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=None,
            kind="template_sent",
            direction="outbound",
            payload={"period": period, "filename": filename},
            response={"status": "build_error", "error": str(error)[:500]},
            period=period,
        )
        return
    try:
        if document_sender:
            document_sender(str(chat_id), document, filename, caption)
        else:
            send_document(token, chat_id, document, filename=filename, caption=caption)
    except TelegramError as error:
        store.log_bot_interaction(
            partner_id=partner["id"],
            chat_id=str(chat_id),
            update_id=None,
            kind="template_sent",
            direction="outbound",
            payload={"period": period, "filename": filename},
            response={"status": "telegram_rejected", "error": str(error)[:500]},
            period=period,
        )
        return
    store.log_bot_interaction(
        partner_id=partner["id"],
        chat_id=str(chat_id),
        update_id=None,
        kind="template_sent",
        direction="outbound",
        payload={"period": period, "filename": filename, "caption": caption},
        response={"status": "sent", "file_size": len(document)},
        period=period,
    )


def send_partner_notice(
    store: Store,
    partner: dict,
    period: str,
    notice_key: str,
    *,
    sender: Sender | None = None,
    bot_token: str | None = None,
    document_sender: DocumentSender | None = None,
    force: bool = False,
) -> dict[str, str]:
    events = {
        event["key"]: event
        for event in schedule_for_period(
            partner["notice_settings"],
            period,
            name=partner["name"],
            link="",
        )
    }
    event = events.get(notice_key)
    if event is None:
        return {"status": "skipped", "detail": "notice_disabled"}
    if should_skip_because_submitted(store, partner["id"], period, notice_key):
        return {"status": "skipped", "detail": "already_submitted"}
    chat_id = partner.get("telegram_chat_id") or ""
    if not chat_id:
        return {"status": "blocked", "detail": "partner_must_start_bot"}
    if not sender and not (bot_token or telegram_token()):
        return {"status": "blocked", "detail": "bot_token_missing"}
    if not store.claim_notice(
        partner["id"], period, notice_key, force=force
    ):
        return {"status": "skipped", "detail": "already_sent"}
    if notice_key == "invite":
        # The Telegram invite uses a separate template (per-partner override
        # from notice_settings.notices or i18n invite_telegram_default).
        # It deliberately does NOT carry the web-form link — the xlsx
        # template attached below already explains the report flow.
        deadline_iso = deadline_date(partner["notice_settings"], period).isoformat()
        text = _resolve_invite_text(partner, period, deadline_iso)
    else:
        text = event["message"].strip()

    try:
        if sender:
            sender(str(chat_id), text)
        else:
            token = bot_token or telegram_token()
            send_message(token, chat_id, text)
    except TelegramError as error:
        store.log_notice_send(
            partner["id"], period, notice_key, "error", str(error)[:500]
        )
        return {"status": "error", "detail": "telegram_rejected"}
    store.log_notice_send(partner["id"], period, notice_key, "sent", "telegram")
    if notice_key == "invite":
        attach_invite_template(
            store, partner, period, chat_id, bot_token, document_sender
        )
    return {"status": "sent", "detail": notice_key}


def send_due_notices(
    store: Store,
    today: date | None = None,
    period: str | None = None,
    *,
    sender: Sender | None = None,
    bot_token: str | None = None,
    kate_sender: Sender | None = None,
) -> list[dict[str, str]]:
    today = today or local_today()
    period = period or reporting_period()
    results = []
    for partner in store.partners():
        for event in due_events(partner, today, period, store):
            if event["audience"] == "partner":
                result = send_partner_notice(
                    store,
                    partner,
                    period,
                    event["key"],
                    sender=sender,
                    bot_token=bot_token,
                )
                results.append({"partner": partner["name"], "key": event["key"], **result})
            elif event["key"] == "digest":
                if not store.claim_notice(0, period, "digest"):
                    continue
                kate_chat = store.digest_chat()
                if not kate_chat:
                    results.append(
                        {
                            "partner": "Internal",
                            "key": "digest",
                            "status": "blocked",
                            "detail": "digest_chat_missing",
                        }
                    )
                    store.log_notice_send(0, period, "digest", "blocked", "digest_chat_missing")
                    continue
                text = digest_text(
                    store, period, store.setting("digest_language", "ru")
                )
                try:
                    if kate_sender:
                        kate_sender(kate_chat, text)
                    elif sender:
                        sender(kate_chat, text)
                    else:
                        token = bot_token or telegram_token()
                        send_message(token, kate_chat, text)
                    store.log_notice_send(0, period, "digest", "sent", "")
                    results.append(
                        {"partner": "Internal", "key": "digest", "status": "sent", "detail": ""}
                    )
                except TelegramError:
                    store.log_notice_send(0, period, "digest", "error", "telegram_rejected")
                    results.append(
                        {
                            "partner": "Internal",
                            "key": "digest",
                            "status": "error",
                            "detail": "telegram_rejected",
                        }
                    )
    return results

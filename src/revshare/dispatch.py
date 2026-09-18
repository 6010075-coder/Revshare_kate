from __future__ import annotations

from datetime import date
from typing import Any, Callable

from revshare.email_letter import SUBJECTS as EMAIL_SUBJECTS
from revshare.email_letter import build_letter
from revshare.mailer import MailError, mail_ready, send_email
from revshare.notifications import (
    deadline_date,
    format_notice,
    reporting_period,
    schedule_for_period,
)
from revshare.secrets import telegram_token
from revshare.timezone import local_today
from revshare.store import Store
from revshare.telegram import TelegramError, send_message


Sender = Callable[[str, str], None]
MailSender = Callable[[str, str, str, str], None]

DEFAULT_PUBLIC_URL = "http://127.0.0.1:43127"


def form_link(token: str, period: str, store: Store | None = None) -> str:
    base = ""
    if store is not None:
        base = store.setting("public_base_url")
    if not base:
        from revshare.secrets import public_base_url

        base = public_base_url()
    base = (base or DEFAULT_PUBLIC_URL).rstrip("/")
    return f"{base}/submit/{token}?period={period}"


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
            link=form_link(partner["token"], period, store),
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
    link = form_link(partner["token"], period, store)
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


def send_partner_notice(
    store: Store,
    partner: dict,
    period: str,
    notice_key: str,
    *,
    sender: Sender | None = None,
    mail_sender: MailSender | None = None,
    bot_token: str | None = None,
    force: bool = False,
) -> dict[str, str]:
    events = {
        event["key"]: event
        for event in schedule_for_period(
            partner["notice_settings"],
            period,
            name=partner["name"],
            link=form_link(partner["token"], period, store),
        )
    }
    event = events.get(notice_key)
    if event is None:
        return {"status": "skipped", "detail": "notice_disabled"}
    if not force and store.notice_already_sent(partner["id"], period, notice_key):
        return {"status": "skipped", "detail": "already_sent"}
    if should_skip_because_submitted(store, partner["id"], period, notice_key):
        return {"status": "skipped", "detail": "already_submitted"}
    text = event["message"].strip()
    link = form_link(partner["token"], period, store)
    if link not in text:
        text = f"{text}\n{link}"
    channel = partner.get("report_channel")

    if channel == "email":
        address = (partner.get("email") or "").strip()
        if not address:
            return {"status": "blocked", "detail": "email_missing"}
        letter = partner_letter(
            partner, period, notice_key, message=event["message"], store=store
        )
        try:
            if mail_sender:
                mail_sender(address, letter["subject"], letter["text"], letter["html"])
            else:
                if not mail_ready():
                    return {"status": "blocked", "detail": "smtp_not_configured"}
                send_email(
                    address, letter["subject"], letter["text"], letter["html"]
                )
        except MailError as error:
            store.log_notice_send(
                partner["id"], period, notice_key, "error", str(error)[:500]
            )
            return {"status": "error", "detail": "email_rejected"}
        store.log_notice_send(partner["id"], period, notice_key, "sent", "email")
        return {"status": "sent", "detail": notice_key}

    if channel != "telegram":
        return {"status": "skipped", "detail": "channel_not_supported"}
    chat_id = partner.get("telegram_chat_id") or ""
    if not chat_id:
        return {"status": "blocked", "detail": "partner_must_start_bot"}
    try:
        if sender:
            sender(str(chat_id), text)
        else:
            token = bot_token or telegram_token()
            if not token:
                return {"status": "blocked", "detail": "bot_token_missing"}
            send_message(token, chat_id, text)
    except TelegramError as error:
        store.log_notice_send(
            partner["id"], period, notice_key, "error", str(error)[:500]
        )
        return {"status": "error", "detail": "telegram_rejected"}
    store.log_notice_send(partner["id"], period, notice_key, "sent", "telegram")
    return {"status": "sent", "detail": notice_key}


def send_due_notices(
    store: Store,
    today: date | None = None,
    period: str | None = None,
    *,
    sender: Sender | None = None,
    mail_sender: MailSender | None = None,
    bot_token: str | None = None,
    kate_sender: Sender | None = None,
) -> list[dict[str, str]]:
    today = today or local_today()
    period = period or reporting_period()
    results = []
    digest_handled = store.notice_already_sent(0, period, "digest")
    for partner in store.partners():
        for event in due_events(partner, today, period, store):
            if event["audience"] == "partner":
                result = send_partner_notice(
                    store,
                    partner,
                    period,
                    event["key"],
                    sender=sender,
                    mail_sender=mail_sender,
                    bot_token=bot_token,
                )
                results.append({"partner": partner["name"], "key": event["key"], **result})
            elif event["key"] == "digest":
                if digest_handled:
                    continue
                digest_handled = True
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
                    results.append(
                        {
                            "partner": "Internal",
                            "key": "digest",
                            "status": "error",
                            "detail": "telegram_rejected",
                        }
                    )
    return results

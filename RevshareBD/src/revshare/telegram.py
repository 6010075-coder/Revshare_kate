from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramError(RuntimeError):
    pass


def api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload or {}).encode()
    request = urllib.request.Request(api_url(token, method), data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode() if error.fp else str(error)
        raise TelegramError(detail) from error
    except urllib.error.URLError as error:
        raise TelegramError(str(error.reason)) from error
    if not body.get("ok"):
        raise TelegramError(str(body.get("description") or body))
    return body["result"]


def get_me(token: str) -> dict[str, Any]:
    return call(token, "getMe")


def send_message(
    token: str,
    chat_id: str | int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return call(token, "sendMessage", payload)


def request_template_keyboard() -> dict[str, Any]:
    """Return the persistent reply keyboard markup with a single button.

    Persistence comes from ``resize_keyboard=True`` + ``one_time_keyboard=False``
    — the standard Telegram recipe for keeping a reply keyboard visible.
    The non-standard ``is_persistent`` field is intentionally NOT set: it is
    not part of the Bot API and is either rejected or silently dropped by
    Telegram clients, which causes the reply keyboard to vanish.
    """
    return {
        "keyboard": [[{"text": "Запросить шаблон"}]],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def send_message_reply(
    token: str,
    chat_id: str | int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return call(token, "sendMessage", payload)


def edit_message_text(
    token: str,
    chat_id: str | int,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit the text of a bot-sent message in place.

    Used by ``handle_callback(select_app)`` to swap the application
    selection keyboard for the confirm/resend keyboard without sending
    a second message. Telegram rejects edits to messages that were sent
    more than 48 hours ago and to messages the bot did not send; the
    caller is expected to fall back to ``send_message_reply`` when this
    raises.
    """
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return call(token, "editMessageText", payload)


def get_file_path(token: str, file_id: str) -> str:
    """Return the file_path on Telegram servers for a file_id."""
    result = call(token, "getFile", {"file_id": file_id})
    return result["file_path"]


def download_file(token: str, file_path: str) -> bytes:
    """Download a file from Telegram's CDN. file_path comes from get_file_path."""
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode() if error.fp else str(error)
        raise TelegramError(detail) from error
    except urllib.error.URLError as error:
        raise TelegramError(str(error.reason)) from error


def send_document(
    token: str,
    chat_id: str | int,
    document: bytes,
    filename: str,
    caption: str = "",
) -> dict[str, Any]:
    """Send a file as a document via multipart/form-data.

    Uses ``requests`` for the multipart body — a hand-rolled ``urllib``
    body worked for years but started being rejected by Telegram with
    "invalid file HTTP URL specified" once the server-side parser
    tightened. ``requests`` always emits a spec-compliant body.
    """
    import requests

    files = {"document": (filename, document, "application/octet-stream")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    try:
        response = requests.post(
            api_url(token, "sendDocument"),
            data=data,
            files=files,
            timeout=60,
        )
    except requests.RequestException as error:
        raise TelegramError(str(error)) from error
    try:
        body_json = response.json()
    except ValueError as error:
        raise TelegramError(f"Non-JSON response from Telegram: {response.text[:200]}") from error
    if not body_json.get("ok"):
        raise TelegramError(str(body_json.get("description") or body_json))
    return body_json["result"]


def answer_callback_query(
    token: str,
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "callback_query_id": callback_query_id,
        "text": text,
    }
    if show_alert:
        payload["show_alert"] = "true"
    return call(token, "answerCallbackQuery", payload)


def get_updates(token: str, offset: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": 20}
    if offset:
        params["offset"] = offset
    url = api_url(token, "getUpdates") + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode() if error.fp else str(error)
        raise TelegramError(detail) from error
    except urllib.error.URLError as error:
        raise TelegramError(str(error.reason)) from error
    if not body.get("ok"):
        raise TelegramError(str(body.get("description") or body))
    return body["result"]

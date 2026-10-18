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


def send_message(token: str, chat_id: str | int, text: str) -> dict[str, Any]:
    return call(
        token,
        "sendMessage",
        {"chat_id": str(chat_id), "text": text},
    )


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

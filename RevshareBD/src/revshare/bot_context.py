from __future__ import annotations

from contextvars import ContextVar


_current_bot_id: ContextVar[int | None] = ContextVar(
    "revshare_current_bot_id", default=None
)
_current_file_id: ContextVar[int | None] = ContextVar(
    "revshare_current_file_id", default=None
)


def set_current_bot_id(bot_id: int | None):
    return _current_bot_id.set(bot_id)


def current_bot_id() -> int | None:
    return _current_bot_id.get()


def set_current_file_id(file_id: int | None):
    return _current_file_id.set(file_id)


def current_file_id() -> int | None:
    return _current_file_id.get()

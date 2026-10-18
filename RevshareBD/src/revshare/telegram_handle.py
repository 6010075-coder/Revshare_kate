"""Normalize Telegram handles and detect whether two values refer to the same account.

Used by the admin partner-edit flow to decide whether a Telegram handle change is
a real switch to a different account or just a cosmetic edit (case, leading ``@``,
``t.me/`` URL form, surrounding whitespace).

Comparison is intentionally case-insensitive: Telegram usernames are
case-insensitive in practice, so ``@NewContact``, ``NEWCONTACT`` and
``https://t.me/newcontact`` must all be treated as the same handle.

The helper also covers the ``set_digest_chat`` flow in ``store.Store`` and
should remain dependency-free so it can be imported from tests without
spinning up the database.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = [
    "normalize_telegram_handle",
    "handles_are_equal",
    "TME_URL_PREFIXES",
]


TME_URL_PREFIXES: tuple[str, ...] = (
    "https://t.me/",
    "http://t.me/",
    "t.me/",
)


def normalize_telegram_handle(value: str | None) -> str:
    """Return the canonical Telegram username for ``value`` or an empty string.

    Canonical form: stripped of surrounding whitespace, with the leading ``@``
    removed, in lowercase, with any ``t.me/<user>`` URL prefix stripped.
    Trailing path slashes are also dropped so ``t.me/foo/`` matches
    ``t.me/foo``. Returns ``""`` for falsy input so callers can compare with
    ``==``.

    Examples::

        >>> normalize_telegram_handle("@NewContact")
        'newcontact'
        >>> normalize_telegram_handle(" NEWCONTACT ")
        'newcontact'
        >>> normalize_telegram_handle("https://t.me/newcontact")
        'newcontact'
        >>> normalize_telegram_handle("t.me/newcontact/")
        'newcontact'
        >>> normalize_telegram_handle("")
        ''
    """
    if not value:
        return ""
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    for prefix in TME_URL_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            lowered = text.lower()
            break
    text = text.lstrip("@").strip().rstrip("/")
    return text.lower()


def handles_are_equal(old: str | None, new: str | None) -> bool:
    """True when the two Telegram handles refer to the same account.

    Empty strings compare equal — this is intentional: if an admin clears the
    Telegram handle, the previous contact is considered "no longer set", which
    is the same practical state as "never set" for the purposes of
    unlinking the chat and revoking invites.
    """
    return normalize_telegram_handle(old) == normalize_telegram_handle(new)


def extract_handle_from_tme_url(url: str) -> str:
    """Extract a Telegram username from a ``t.me`` style URL.

    Returns the username (without ``@``) or an empty string when the URL does
    not look like a Telegram link. Path components after the username
    (e.g. ``?start=token`` query parameters, trailing slashes) are discarded.

    Used by both the admin form (parsing user input) and the dispatcher (in
    case a future flow receives a URL instead of a bare handle).
    """
    if not url:
        return ""
    text = url.strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.netloc or parsed.path.split("/", 1)[0]).lower()
    if host not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return ""
    parts = [segment for segment in parsed.path.split("/") if segment]
    if not parts:
        return ""
    return parts[0].lstrip("@").strip().lower()
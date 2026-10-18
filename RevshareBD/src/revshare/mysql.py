from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from revshare.secrets import load_secrets


def mysql_configured() -> bool:
    load_secrets()
    return bool(_host() and _user() and _password())


def _host() -> str:
    return os.getenv("ALTEGIO_MYSQL_HOST", "").strip()


def _port() -> int:
    raw = os.getenv("ALTEGIO_MYSQL_PORT", "3306").strip()
    try:
        return int(raw)
    except ValueError:
        return 3306


def _user() -> str:
    return os.getenv("ALTEGIO_MYSQL_USER", "").strip()


def _password() -> str:
    return os.getenv("ALTEGIO_MYSQL_PASSWORD", "").strip()


def _database() -> str:
    return os.getenv("ALTEGIO_MYSQL_DATABASE", "salon").strip() or "salon"


@contextmanager
def mysql_connection() -> Iterator[object]:
    if not mysql_configured():
        raise RuntimeError("altegio_mysql_missing")
    import pymysql

    connection = pymysql.connect(
        host=_host(),
        port=_port(),
        user=_user(),
        password=_password(),
        database=_database(),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
    )
    try:
        yield connection
    finally:
        connection.close()

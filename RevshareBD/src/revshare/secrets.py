from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = ROOT / "data" / "secrets.env"


def load_secrets(path: Path | None = None) -> None:
    file_path = path or SECRETS_PATH
    if not file_path.exists():
        return
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def telegram_token() -> str:
    load_secrets()
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def public_base_url() -> str:
    load_secrets()
    return os.getenv("REVSHARE_PUBLIC_URL", "").rstrip("/")

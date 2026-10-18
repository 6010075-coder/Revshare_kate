from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = ROOT / "data" / "secrets.env"


def load_secrets(path: Path | None = None) -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        # Tests must not leak a developer's local secrets into the suite.
        return
    file_path = path or SECRETS_PATH
    if not file_path.exists():
        return
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _file_secret(key: str, path: Path | None = None) -> str | None:
    file_path = path or SECRETS_PATH
    if not file_path.exists():
        return None
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip()
    return None


def set_secret(key: str, value: str, path: Path | None = None) -> None:
    """Update one secret atomically while preserving the other secret entries."""
    file_path = path or SECRETS_PATH
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines = file_path.read_text(encoding="utf-8").splitlines() if file_path.exists() else []
    replaced = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if name == key:
                output.append(f"{key}={value}")
                replaced = True
                continue
        output.append(line)
    if not replaced:
        if output and output[-1] != "":
            output.append("")
        output.append(f"{key}={value}")
    content = "\n".join(output).rstrip("\n") + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{file_path.name}.", dir=file_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, file_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    os.environ[key] = value


def telegram_token() -> str:
    # Once the local secrets file exists, it is the source of truth for the
    # admin-managed token. An empty value intentionally disables the bot even
    # if an old token is still present in the process environment.
    if not os.getenv("PYTEST_CURRENT_TEST"):
        from_file = _file_secret("TELEGRAM_BOT_TOKEN")
        if from_file is not None:
            return from_file
    load_secrets()
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def public_base_url() -> str:
    load_secrets()
    return os.getenv("REVSHARE_PUBLIC_URL", "").rstrip("/")

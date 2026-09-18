from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def backup_data(source: Path, destination: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = destination / stamp
    target.mkdir(parents=True, exist_ok=False)

    database = source / "revshare.db"
    if database.exists():
        with sqlite3.connect(database) as connection:
            with sqlite3.connect(target / "revshare.db") as copy:
                connection.backup(copy)

    uploads = source / "uploads"
    if uploads.exists():
        shutil.copytree(uploads, target / "uploads")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent Revshare data backup")
    parser.add_argument("--source", type=Path, default=ROOT / "data")
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    target = backup_data(args.source, args.destination)
    print(target)


if __name__ == "__main__":
    main()

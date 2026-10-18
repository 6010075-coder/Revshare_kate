from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = ROOT / "partners.yaml"


@dataclass
class Partner:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    channel: str = "mixed"
    currency: str | None = None
    expected_by_day: int = 10
    notes: str = ""
    sheet: dict[str, Any] = field(default_factory=dict)

    def matches(self, text: str) -> bool:
        hay = (text or "").casefold()
        names = [self.id, self.name, *self.aliases]
        return any(n and n.casefold() in hay for n in names)


@dataclass
class Registry:
    sheet: dict[str, Any]
    write_policy: dict[str, Any]
    deadline: dict[str, Any]
    partners: list[Partner]
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> Registry:
        registry_path = path or DEFAULT_REGISTRY
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        partners = [Partner(**item) for item in (raw.get("partners") or [])]
        return cls(
            sheet=raw.get("sheet") or {},
            write_policy=raw.get("write_policy") or {},
            deadline=raw.get("deadline") or {},
            partners=partners,
            path=registry_path,
        )

    def match(self, text: str) -> list[Partner]:
        return [p for p in self.partners if p.matches(text)]

    def missing(self, received_ids: set[str]) -> list[Partner]:
        return [p for p in self.partners if p.id not in received_ids]


def month_label(month: str) -> str:
    """Accept YYYY-MM and return a short human label."""
    year, mm = month.split("-")
    names = {
        "01": "January",
        "02": "February",
        "03": "March",
        "04": "April",
        "05": "May",
        "06": "June",
        "07": "July",
        "08": "August",
        "09": "September",
        "10": "October",
        "11": "November",
        "12": "December",
    }
    return f"{names[mm]} {year}"

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from revshare.extract import ExtractedReport
from revshare.registry import Registry

ROOT = Path(__file__).resolve().parents[2]


def _year_tab(registry: Registry, month: str) -> dict | None:
    year = int(month.split("-")[0])
    tabs = registry.sheet.get("year_tabs") or {}
    return tabs.get(year) or tabs.get(str(year))


def build_draft(
    month: str,
    reports: list[ExtractedReport],
    registry: Registry,
) -> dict:
    received_ids: set[str] = set()
    items = []
    for report in reports:
        matches = registry.match(report.partner_hint) or registry.match(report.source)
        partner = matches[0] if len(matches) == 1 else None
        if partner:
            received_ids.add(partner.id)
        best = next((c for c in report.candidates if c.confidence == "medium"), None)
        if best is None and len(report.candidates) == 1:
            best = report.candidates[0]
        items.append(
            {
                "source": report.source,
                "partner_hint": report.partner_hint,
                "matched_partner_id": None if partner is None else partner.id,
                "matched_partner_name": None if partner is None else partner.name,
                "ambiguous_matches": [p.id for p in matches] if len(matches) > 1 else [],
                "proposed_amount": None if best is None else best.amount,
                "proposed_currency": None if best is None else best.currency,
                "confidence": None if best is None else best.confidence,
                "candidates": [c.__dict__ for c in report.candidates],
                "notes": report.notes,
                "error": report.error,
                "sheet_write": {
                    "spreadsheet_id": registry.sheet.get("id"),
                    "tab": _year_tab(registry, month),
                    "row": None if partner is None else (partner.sheet or {}).get("row"),
                    "was": None,
                    "will_be": None if best is None else best.amount,
                    "blocked_until": "explicit approval required",
                },
            }
        )

    missing = [
        {"id": p.id, "name": p.name, "channel": p.channel}
        for p in registry.missing(received_ids)
    ]
    return {
        "month": month,
        "created_at": datetime.now(UTC).isoformat(),
        "sheet": registry.sheet,
        "items": items,
        "missing_partners": missing,
        "unmapped_partners": len(registry.partners) == 0,
        "status": "preview_only",
    }


def save_draft(draft: dict, month: str) -> Path:
    folder = ROOT / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{month}.json"
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

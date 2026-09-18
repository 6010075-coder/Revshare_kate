from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from revshare.secrets import load_secrets
from revshare.store import Store

ROOT = Path(__file__).resolve().parents[2]


def snapshot_url() -> str:
    load_secrets()
    return os.getenv("ALTEGIO_SNAPSHOT_URL", "").strip()


def snapshot_token() -> str:
    load_secrets()
    return os.getenv("ALTEGIO_API_TOKEN", "").strip()


def local_snapshot_path(period: str) -> Path:
    return ROOT / "data" / "altegio" / f"{period}.json"


def parse_location_details(raw: object) -> dict[str, dict[str, object]]:
    if not raw:
        return {}
    if isinstance(raw, list):
        details: dict[str, dict[str, object]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            location_id = str(item.get("id") or item.get("location_id") or "").strip()
            if location_id:
                details[location_id] = item
        return details
    if isinstance(raw, dict):
        return {
            str(location_id): value if isinstance(value, dict) else {"name": value}
            for location_id, value in raw.items()
        }
    return {}


def parse_snapshot_payload(raw: dict) -> list[dict]:
    period = str(raw.get("period") or "")
    rows = raw.get("partners") or raw.get("items") or []
    parsed = []
    for item in rows:
        parsed.append(
            {
                "period": str(item.get("period") or period),
                "partner_id": item.get("partner_id"),
                "token": str(item.get("token") or "").strip(),
                "connected": [str(value) for value in item.get("connected") or []],
                "active": [str(value) for value in item.get("active") or []],
                "payment": [str(value) for value in item.get("payment") or []],
                "location_details": parse_location_details(item.get("locations")),
            }
        )
    return parsed


def load_snapshot_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_snapshot(period: str) -> dict:
    url = snapshot_url().replace("{period}", period)
    if not url:
        raise ValueError("altegio_url_missing")
    headers = {}
    token = snapshot_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def apply_snapshot(
    store: Store,
    payload: dict,
    *,
    source: str,
    default_period: str = "",
) -> dict:
    rows = parse_snapshot_payload(payload)
    imported = 0
    missing = []
    for item in rows:
        period = item["period"] or default_period
        partner = None
        if item["partner_id"]:
            partner = store.partner(int(item["partner_id"]))
        if partner is None and item["token"]:
            partner = store.partner_by_token(item["token"])
        if not partner or not period:
            missing.append(item["token"] or str(item["partner_id"] or ""))
            continue
        store.replace_usage(
            partner["id"],
            period,
            item["connected"],
            item["active"],
            item["payment"],
            source=source,
            location_details=item["location_details"],
        )
        imported += 1
    return {"imported": imported, "missing": [value for value in missing if value]}

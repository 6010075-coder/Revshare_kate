from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from revshare.altegio_sync import parse_application_id


KNOWN_APPLICATION_NAMES = {
    25: "NotiSend",
    26: "Kompaas",
    34: "Smartmetrika",
    36: "Flowsell",
    37: "SMSPRO.KG",
    38: "AssistBot",
    39: "Message.help",
    41: "Smartmetrika",
    46: "Sms.to",
    49: "Fromni",
    51: "Boomerangme",
    54: "KPIOrg",
    56: "Wazapa",
    61: "WaxSms",
    63: "Beautybot",
    71: "Revvy",
    74: "Phonet",
    78: "Smartmetrika",
    80: "SMS.md",
    82: "Chat app",
    83: "Chat app",
    91: "Apifonica",
    98: "Bondus",
    100: "KommoCRM",
    109: "AlphaSMS",
    121: "Fromni Conversations",
    1307: "Solwees.ai",
    1372: "Stiker.ai",
    1436: "Fast sing",
    1575: "Staffono.ai",
    1681: "Dconnect",
    1786: "Ziflow",
    1826: "Autopayroll",
    2162: "MesseDo",
    2337: "Chat app",
}


def _fold(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _link_id(link: dict[str, str]) -> int | None:
    return parse_application_id(link.get("url", ""))


def _link_label(link: dict[str, str]) -> str:
    comment = str(link.get("comment") or "").strip()
    if comment:
        return comment
    application_id = _link_id(link)
    if application_id in KNOWN_APPLICATION_NAMES:
        return KNOWN_APPLICATION_NAMES[application_id]
    return str(link.get("url") or "").strip()


def _match_link(ref: str, links: list[dict[str, str]]) -> dict[str, str] | None:
    ref_id = parse_application_id(ref)
    folded_ref = _fold(ref)
    if ref_id is not None:
        for link in links:
            if _link_id(link) == ref_id:
                return link
    if folded_ref:
        for link in links:
            label = _fold(_link_label(link))
            url = _fold(link.get("url"))
            if folded_ref == label or folded_ref == url:
                return link
        for link in links:
            label = _fold(_link_label(link))
            url = _fold(link.get("url"))
            if len(folded_ref) >= 4 and (folded_ref in label or folded_ref in url):
                return link
    return None


def enrich_application_breakdown(
    items: Iterable[dict[str, Any]] | None,
    links: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Attach configured APP links to extracted application blocks.

    A report may identify an application by its APP URL, numeric id, or name.
    A legacy report with no application column is assigned to its only configured
    application; with several applications it stays explicitly unmatched.
    """
    raw_items = list(items or [])
    if not raw_items:
        return []
    result: list[dict[str, Any]] = []
    for item in raw_items:
        row = dict(item)
        ref = str(row.get("application_ref") or "").strip()
        matched = _match_link(ref, links)
        if not ref and len(links) == 1:
            matched = links[0]
        row["application_ref"] = ref
        row["application_url"] = matched.get("url", "") if matched else ""
        row["application_name"] = _link_label(matched) if matched else ref
        row["application_id"] = _link_id(matched) if matched else parse_application_id(ref)
        row["application_match"] = "matched" if matched else "unmatched"
        row["location_ids"] = list(dict.fromkeys(str(x) for x in row.get("location_ids", [])))
        result.append(row)
    return result


def breakdown_for_report(parsed: Any, links: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Return app blocks for both the new template and legacy reports."""
    items = list(getattr(parsed, "application_breakdown", None) or [])
    if not items:
        items = [
            {
                "application_ref": "",
                "turnover": getattr(parsed, "turnover", None),
                "reported_share": getattr(parsed, "reported_share", None),
                "currency": getattr(parsed, "currency", None),
                "location_ids": list(getattr(parsed, "location_ids", None) or []),
            }
        ]
    return enrich_application_breakdown(items, links)


def add_usage_checks(
    items: Iterable[dict[str, Any]] | None,
    usage_by_application: dict[int, tuple[set[str], set[str]] | None],
) -> list[dict[str, Any]]:
    """Add per-APP Location ID comparison when an APP snapshot exists."""
    result: list[dict[str, Any]] = []
    for raw in items or []:
        item = dict(raw)
        app_id = item.get("application_id")
        usage = usage_by_application.get(app_id) if isinstance(app_id, int) else None
        reported = set(item.get("location_ids") or [])
        item["application_usage_ready"] = usage is not None
        if usage is None:
            item["application_connected_count"] = None
            item["application_active_count"] = None
            item["application_missing_active"] = []
            item["application_unknown_locations"] = []
        else:
            connected, active = usage
            item["application_connected_count"] = len(connected)
            item["application_active_count"] = len(active)
            item["application_missing_active"] = sorted(active - reported)
            item["application_unknown_locations"] = sorted(reported - connected)
        result.append(item)
    return result

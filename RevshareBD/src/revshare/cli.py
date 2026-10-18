from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from revshare.extract import ingest_dir
from revshare.preview import build_draft, save_draft
from revshare.registry import ROOT, Registry, month_label


def _month(value: str) -> str:
    parts = value.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise argparse.ArgumentTypeError("month must be YYYY-MM")
    return value


def cmd_status(args: argparse.Namespace) -> int:
    registry = Registry.load()
    inbox = ROOT / "inbox" / args.month
    files = []
    if inbox.exists():
        files = [
            p.name
            for p in sorted(inbox.iterdir())
            if p.is_file() and not p.name.startswith(".") and p.name != ".gitkeep"
        ]
    payload = {
        "month": args.month,
        "label": month_label(args.month),
        "sheet": registry.sheet.get("url"),
        "partners_in_registry": [
            {"id": p.id, "name": p.name, "channel": p.channel} for p in registry.partners
        ],
        "inbox_dir": str(inbox),
        "inbox_files": files,
        "note": (
            "Registry is empty until the first mapped month. "
            "Drop files into inbox/<YYYY-MM>/ then run ingest."
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    folder = Path(args.path)
    if not folder.exists():
        print(f"inbox not found: {folder}", file=sys.stderr)
        return 1
    reports = ingest_dir(folder)
    registry = Registry.load()
    draft = build_draft(args.month, reports, registry)
    path = save_draft(draft, args.month)
    print(json.dumps({"draft": str(path), **draft}, ensure_ascii=False, indent=2))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    path = ROOT / "drafts" / f"{args.month}.json"
    if not path.exists():
        print(f"no draft for {args.month}; run ingest first", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_sync_altegio(args: argparse.Namespace) -> int:
    """Deprecated: Altegio BI sync is a no-op post-pivot.

    Partner reports are now reconciled against the monthly base file that
    the admin uploads through the admin page (``/admin/reconciliation``).
    This command is kept so existing automation does not crash with an
    unknown subcommand error, but it does nothing.
    """
    print(
        json.dumps(
            {
                "ok": False,
                "deprecated": True,
                "message": (
                    "Altegio BI sync is deprecated. Upload the monthly "
                    "reconciliation base through the admin upload page "
                    "instead."
                ),
                "period": args.month,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1


def cmd_import_usage(args: argparse.Namespace) -> int:
    from revshare.altegio import apply_snapshot, fetch_snapshot, load_snapshot_file
    from revshare.secrets import load_secrets
    from revshare.store import Store

    load_secrets()
    store = Store()
    if args.file:
        payload = load_snapshot_file(Path(args.file))
        source = "json"
    else:
        payload = fetch_snapshot(args.month)
        source = "api"
    result = apply_snapshot(store, payload, source=source, default_period=args.month)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["imported"] else 1


def cmd_sheet_probe(args: argparse.Namespace) -> int:
    from revshare import sheets
    from revshare.secrets import load_secrets

    load_secrets()
    if not sheets.credentials_ready():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "google_credentials_missing",
                    "hint": (
                        "Add GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE "
                        "to data/secrets.env and share the sheet with the service account."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    try:
        payload = sheets.probe_sheet(args.month)
        payload["ok"] = payload.get("month_column") is not None
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 2
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1


def cmd_import_partners(args: argparse.Namespace) -> int:
    from revshare.sheet_partners import import_partners_from_sheet
    from revshare.store import Store

    rows = None
    if args.rows_file:
        rows = json.loads(Path(args.rows_file).read_text(encoding="utf-8"))
    result = import_partners_from_sheet(
        Store(),
        year=args.year,
        rows=rows,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "created": result.created,
                "updated": result.updated,
                "created_count": len(result.created),
                "updated_count": len(result.updated),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_sheet_flush(args: argparse.Namespace) -> int:
    from revshare import sheets
    from revshare.secrets import load_secrets
    from revshare.sheet_sync import flush_queued_sheet_writes
    from revshare.store import Store

    load_secrets()
    if not sheets.credentials_ready():
        print("google_credentials_missing", file=sys.stderr)
        return 1
    results = flush_queued_sheet_writes(Store())
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in results) else 1


def cmd_send_notices(args: argparse.Namespace) -> int:
    from revshare.dispatch import send_due_notices
    from revshare.secrets import load_secrets
    from revshare.store import Store

    load_secrets()
    from revshare.timezone import local_today

    today = date.fromisoformat(args.date) if args.date else local_today()
    results = send_due_notices(Store(), today=today, period=args.month, source="manual")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revshare")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--month", type=_month, required=True)
    status.set_defaults(func=cmd_status)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("path")
    ingest.add_argument("--month", type=_month, required=True)
    ingest.set_defaults(func=cmd_ingest)

    preview = sub.add_parser("preview")
    preview.add_argument("--month", type=_month, required=True)
    preview.set_defaults(func=cmd_preview)

    send_notices = sub.add_parser("send-notices")
    send_notices.add_argument("--month", type=_month)
    send_notices.add_argument("--date")
    send_notices.set_defaults(func=cmd_send_notices)

    import_usage = sub.add_parser("import-usage")
    import_usage.add_argument("--month", type=_month, required=True)
    import_usage.add_argument("--file")
    import_usage.set_defaults(func=cmd_import_usage)

    sync_altegio = sub.add_parser("sync-altegio")
    sync_altegio.add_argument("--month", type=_month, required=True)
    sync_altegio.set_defaults(func=cmd_sync_altegio)

    import_partners = sub.add_parser("import-partners")
    import_partners.add_argument("--year", default="2026")
    import_partners.add_argument("--dry-run", action="store_true")
    import_partners.add_argument(
        "--rows-file",
        help="Optional JSON file with raw sheet rows (A:AI) when Google credentials are offline",
    )
    import_partners.set_defaults(func=cmd_import_partners)

    sheet_probe = sub.add_parser("sheet-probe")
    sheet_probe.add_argument("--month", type=_month, required=True)
    sheet_probe.set_defaults(func=cmd_sheet_probe)

    sheet_flush = sub.add_parser("sheet-flush")
    sheet_flush.set_defaults(func=cmd_sheet_flush)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

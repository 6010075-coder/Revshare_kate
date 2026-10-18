"""Import Integration Hub partners from the Partnerships Altegio Revenue Accrual sheet."""

from __future__ import annotations

import argparse

from revshare.sheet_partners import import_partners_from_sheet
from revshare.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", default="2026")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    store = Store()
    result = import_partners_from_sheet(store, year=args.year, dry_run=args.dry_run)
    print(f"created ({len(result.created)}): {', '.join(result.created) or '-'}")
    print(f"updated ({len(result.updated)}): {', '.join(result.updated) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

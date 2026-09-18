from __future__ import annotations

from revshare import sheets
from revshare.store import Store


def flush_queued_sheet_writes(store: Store) -> list[dict]:
    if not sheets.credentials_ready():
        return []
    results = []
    for item in store.sheet_writes_by_status("queued"):
        try:
            write_result = sheets.write_turnover(
                partner_name=item["partner_name"],
                period=item["period"],
                turnover=float(item["turnover"]),
                currency=item["currency"],
            )
        except Exception as exc:
            write_result = sheets.SheetWriteResult(ok=False, error=str(exc))
        if write_result.ok:
            detail = f"Written to {write_result.cell}"
            if write_result.was not in (None, "", write_result.written):
                detail = (
                    f"{write_result.cell}: {write_result.was} -> {write_result.written}"
                )
            store.update_sheet_write(
                item["id"],
                status="written",
                detail=detail,
                sheet_cell=write_result.cell,
            )
        else:
            store.update_sheet_write(
                item["id"],
                status="failed",
                detail=write_result.error or "write_failed",
                sheet_cell=write_result.cell,
            )
        results.append(
            {
                "id": item["id"],
                "partner_name": item["partner_name"],
                **write_result.__dict__,
            }
        )
    return results

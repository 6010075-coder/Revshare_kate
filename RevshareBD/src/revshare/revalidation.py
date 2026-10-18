"""Background revalidation of partner reports after a base upload.

Step 3 of the Integration Hub Revshare pivot: when an admin uploads (or
replaces) a reconciliation base, every existing report for that period must
be re-evaluated against the new ``connected_ids`` / ``active_ids``. The
work happens in a daemon thread so the upload response is not blocked.

The worker is intentionally tiny:

* The Store is the only collaborator.
* No Flask context is held — the thread reads / writes SQLite directly.
* Per-report failures are logged and counted; the loop never dies on one
  bad row.
* The sleep between reports is a module-level constant so tests can
  override it to ``0`` and avoid real wall-clock waits.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime

from revshare.store import Store, assess

log = logging.getLogger("revshare.revalidation")

# Delay inserted between report updates so the polling progress bar visibly
# moves during normal operation. Tests override this to ``0`` to keep the
# suite fast.
STEP_SLEEP_SECONDS = 0.08

# Module-level registry of live threads, keyed by task id. Used to:
#  - guard against starting a duplicate thread for the same task id;
#  - let tests (or future shutdown logic) ``.join()`` a running task.
_active_threads: dict[str, threading.Thread] = {}
_active_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _set_task_running(store: Store, task_id: str, total: int) -> None:
    store.update_revalidation_task(
        task_id,
        status="running",
        total=total,
        started_at=_utc_now_iso(),
    )


def _set_task_done(
    store: Store,
    task_id: str,
    summary: dict,
) -> None:
    store.update_revalidation_task(
        task_id,
        status="done",
        finished_at=_utc_now_iso(),
        summary=json.dumps(summary, ensure_ascii=False),
    )


def _cancel_stale_tasks(store: Store, period: str) -> int:
    """Mark any pending/running tasks for ``period`` as cancelled.

    Called right before creating a fresh task. The worker in the cancelled
    task is allowed to keep running — it only writes to the ``reports``
    table which is independent — but its progress row reflects the
    cancellation so the UI does not show stale numbers.
    """
    return store.cancel_revalidation_tasks(period)


def schedule_revalidation(
    store: Store,
    period: str,
) -> str:
    """Cancel any in-flight tasks for ``period`` and start a new one.

    Returns the freshly created ``task_id``. The thread is daemonised so
    process shutdown does not wait on it.
    """
    _cancel_stale_tasks(store, period)
    task_id = uuid.uuid4().hex
    store.create_revalidation_task(task_id, period)
    thread = threading.Thread(
        target=run_revalidation,
        args=(store, task_id, period),
        daemon=True,
        name=f"revalidation-{period}-{task_id[:8]}",
    )
    with _active_lock:
        _active_threads[task_id] = thread
    thread.start()
    return task_id


def is_task_running(task_id: str) -> bool:
    """Return ``True`` if a worker thread for ``task_id`` is still alive."""
    with _active_lock:
        thread = _active_threads.get(task_id)
    return bool(thread and thread.is_alive())


def wait_for_task(task_id: str, timeout: float = 10.0) -> bool:
    """Block until the worker for ``task_id`` finishes or ``timeout`` elapses.

    Returns ``True`` when the worker thread exited cleanly within the
    timeout. Used by tests.
    """
    with _active_lock:
        thread = _active_threads.get(task_id)
    if thread is None:
        return True
    thread.join(timeout=timeout)
    return not thread.is_alive()


def run_revalidation(
    store: Store,
    task_id: str,
    period: str,
    *,
    step_sleep: float = STEP_SLEEP_SECONDS,
) -> None:
    """Re-evaluate every report in ``period`` against the current base.

    Public entry point. ``web.py`` calls ``schedule_revalidation`` which
    wraps this in a thread; tests call this directly (with
    ``step_sleep=0``) to drive the loop in-process.
    """
    report_ids = store.list_reports_for_period(period)
    total = len(report_ids)

    if total == 0:
        _set_task_done(
            store,
            task_id,
            {
                "recalculated": 0,
                "status_changed": 0,
                "errors": 0,
                "total": 0,
            },
        )
        return

    _set_task_running(store, task_id, total)

    # Build the connected / active id sets from the **current** base rows.
    # If a base is uploaded mid-run, last-write-wins on the rows table is
    # acceptable — the next revalidation will catch the difference.
    base_rows = store.get_reconciliation_base_rows(period)
    base_meta = store.get_reconciliation_base(period)
    if not base_meta or not base_rows:
        # Base was removed between schedule and run; bail out cleanly.
        _set_task_done(
            store,
            task_id,
            {
                "recalculated": 0,
                "status_changed": 0,
                "errors": 0,
                "total": total,
                "skipped_no_base": True,
            },
        )
        return

    connected_ids = {str(row["location_id"]) for row in base_rows}
    active_ids = {
        str(row["location_id"]) for row in base_rows if int(row["active"])
    }

    summary = {
        "recalculated": 0,
        "status_changed": 0,
        "errors": 0,
        "total": total,
    }

    for index, report_id in enumerate(report_ids, start=1):
        try:
            _revalidate_one_report(
                store=store,
                task_id=task_id,
                report_id=report_id,
                period=period,
                connected_ids=connected_ids,
                active_ids=active_ids,
                summary=summary,
            )
        except Exception:
            log.exception(
                "Revalidation failed for report %s in period %s",
                report_id,
                period,
            )
            summary["errors"] += 1
        # Always advance progress so the bar visibly moves even on errors.
        current_label = _current_report_label(store, report_id)
        store.update_revalidation_task(
            task_id,
            processed=index,
            current_report=current_label,
        )
        if step_sleep:
            time.sleep(step_sleep)

    _set_task_done(store, task_id, summary)


def _revalidate_one_report(
    *,
    store: Store,
    task_id: str,
    report_id: int,
    period: str,
    connected_ids: set[str],
    active_ids: set[str],
    summary: dict,
) -> None:
    """Recompute ``assess`` for a single report and persist the result."""
    report = store.get_report_for_revalidation(report_id)
    if report is None:
        summary["errors"] += 1
        return
    if report.get("report_source") == "manual":
        # Manual reports do not get re-evaluated against a registry.
        return

    location_ids = store.get_report_clients(report_id)
    review = assess(
        location_ids,
        connected_ids,
        active_ids,
        turnover=report["turnover"],
        reported_share=report["reported_share"],
        contract_rate=report["contract_rate"],
        previous_turnover=report["previous_turnover"],
        previous_client_count=report["previous_client_count"],
    )
    store.update_report_assessment(
        report_id=report_id,
        level=review.level,
        score=review.score,
        reasons_json=json.dumps(review.reasons, ensure_ascii=False),
        missing_active_json=json.dumps(review.missing_active),
        unknown_locations_json=json.dumps(review.unknown),
    )
    summary["recalculated"] += 1
    if review.level != report.get("risk_level"):
        summary["status_changed"] += 1


def _current_report_label(store: Store, report_id: int) -> str:
    """Build the human label shown in the progress bar.

    Falls back to a bare ``report {id}`` if the row vanished mid-run.
    """
    report = store.get_report_for_revalidation(report_id)
    if report is None:
        return f"report {report_id}"
    name = str(report.get("partner_name") or "").strip() or "Unknown partner"
    return f"{name} (report {report_id})"
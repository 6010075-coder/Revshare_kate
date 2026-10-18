"""Step 3 revalidation worker — end-to-end.

When an admin uploads (or replaces) a reconciliation base, every
existing report for that period must be re-evaluated against the new
``connected_ids`` / ``active_ids``. The worker runs in a daemon thread
that ``schedule_revalidation`` starts; these tests exercise the worker
in-process by overriding ``STEP_SLEEP_SECONDS`` and waiting for the
thread to finish.

The coverage here mirrors what the brief asks for:

* Insert N reports for a period, seed a base, run the worker — assert
  every report was recomputed and the task row reaches ``done``.
* Pre-set a different ``risk_level`` on one report so the worker must
  flip it — assert ``summary.status_changed == 1``.
* Start a revalidation, then trigger a second one for the same period —
  assert the first is marked ``cancelled`` and the second advances.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from revshare import revalidation
from revshare.extract import BaseRow
from revshare.store import Store
from revshare.web import create_app


def _seed_base(
    store: Store,
    period: str,
    *,
    connected_ids: list[str],
    active_ids: list[str],
    application_id: int = 1,
) -> None:
    rows: list[BaseRow] = []
    seen: set[str] = set()
    for location_id in connected_ids:
        if location_id in seen:
            continue
        seen.add(location_id)
        rows.append(
            BaseRow(
                application_id=application_id,
                location_id=location_id,
                turnover=0.0,
                currency="USD",
                active=1 if location_id in active_ids else 0,
                source_row=len(rows) + 1,
            )
        )
    store.replace_reconciliation_base(
        period=period,
        uploaded_at="2026-09-01T00:00:00+00:00",
        uploaded_by="tests",
        file_path="tests/base.csv",
        rows=rows,
        notes="",
    )


def _make_three_reports(
    store: Store, period: str, location_ids: list[str]
) -> list[int]:
    """Insert three partner reports for ``period`` (different partners).

    Each report declares the same full set of location ids that the
    base will contain. The worker's ``assess`` call then produces a
    ``green`` result (no missing-active flags) and the initial
    ``risk_level`` (``green``) is unchanged — unless a test deliberately
    pre-sets one report to a different level to trigger a status flip.
    """
    partner = store.partner_by_token("demo-partner")
    extra_a = store.add_partner(
        name="Bravo",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        rate=0.3,
        currency="USD",
        token="bravo-token",
    )
    extra_b = store.add_partner(
        name="Charlie",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        rate=0.3,
        currency="USD",
        token="charlie-token",
    )
    partner_ids = [partner["id"], extra_a, extra_b]
    report_ids: list[int] = []
    with store.connect() as db:
        for partner_id in partner_ids:
            cursor = db.execute(
                """
                INSERT INTO reports
                (partner_id, period, turnover, currency, client_count, status,
                risk_level, risk_score, reasons, missing_active, unknown_locations,
                submitted_at, report_source)
                VALUES (?, ?, 100, 'USD', ?, 'verified', 'green', 0, '[]',
                        '[]', '[]', '2026-08-31T00:00:00+00:00', 'partner_file')
                """,
                (partner_id, period, len(location_ids)),
            )
            report_id = int(cursor.lastrowid)
            for location_id in location_ids:
                db.execute(
                    "INSERT OR IGNORE INTO report_clients (report_id, location_id) VALUES (?, ?)",
                    (report_id, location_id),
                )
            report_ids.append(report_id)
    return report_ids


def test_worker_revalidates_every_report(tmp_path) -> None:
    """All three reports get a fresh assessment and the task ends as ``done``."""
    app = create_app(tmp_path / "test.db")
    store: Store = app.state.store
    period = "2026-08"
    # Each report declares all three base locations so ``assess`` returns
    # ``green`` and the worker keeps the initial risk_level unchanged.
    report_ids = _make_three_reports(store, period, ["101", "102", "103"])
    _seed_base(
        store,
        period,
        connected_ids=["101", "102", "103"],
        active_ids=["101", "102", "103"],
    )

    original_sleep = revalidation.STEP_SLEEP_SECONDS
    revalidation.STEP_SLEEP_SECONDS = 0
    try:
        task_id = revalidation.schedule_revalidation(store, period)
        # The thread is daemonised; ``wait_for_task`` joins it directly
        # so we don't need to sleep real time.
        assert revalidation.wait_for_task(task_id, timeout=10)
    finally:
        revalidation.STEP_SLEEP_SECONDS = original_sleep

    task = store.get_revalidation_task(task_id)
    assert task is not None
    assert task["status"] == "done"
    assert task["total"] == 3
    assert task["processed"] == 3
    assert task["summary"]["recalculated"] == 3
    assert task["summary"]["status_changed"] == 0
    assert task["summary"]["errors"] == 0
    assert task["started_at"]
    assert task["finished_at"]

    # Every report has fresh assessment fields populated by the worker.
    with store.connect() as db:
        for report_id in report_ids:
            row = db.execute(
                "SELECT risk_level, risk_score, status FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            # Each report has all active locations and reports them → green.
            assert row["risk_level"] == "green"
            assert row["status"] == "verified"


def test_worker_detects_status_change(tmp_path) -> None:
    """Pre-setting ``risk_level`` differently forces ``status_changed`` to bump."""
    app = create_app(tmp_path / "test.db")
    store: Store = app.state.store
    period = "2026-08"
    report_ids = _make_three_reports(store, period, ["101", "102", "103"])
    _seed_base(
        store,
        period,
        connected_ids=["101", "102", "103"],
        active_ids=["101", "102", "103"],
    )
    # Force the second report to ``red`` so the worker's recompute
    # (which produces ``green``) registers a status change. The other
    # two stay at ``green`` so the bump is exactly +1.
    with store.connect() as db:
        db.execute(
            "UPDATE reports SET risk_level = 'red' WHERE id = ?",
            (report_ids[1],),
        )

    original_sleep = revalidation.STEP_SLEEP_SECONDS
    revalidation.STEP_SLEEP_SECONDS = 0
    try:
        task_id = revalidation.schedule_revalidation(store, period)
        assert revalidation.wait_for_task(task_id, timeout=10)
    finally:
        revalidation.STEP_SLEEP_SECONDS = original_sleep

    task = store.get_revalidation_task(task_id)
    assert task is not None
    assert task["status"] == "done"
    assert task["summary"]["recalculated"] == 3
    assert task["summary"]["status_changed"] == 1


def test_second_run_cancels_the_previous_task(tmp_path) -> None:
    """A new upload while the first worker is still running cancels it."""
    app = create_app(tmp_path / "test.db")
    store: Store = app.state.store
    period = "2026-08"
    _make_three_reports(store, period, ["101", "102", "103"])
    _seed_base(
        store,
        period,
        connected_ids=["101", "102", "103"],
        active_ids=["101", "102", "103"],
    )

    original_sleep = revalidation.STEP_SLEEP_SECONDS
    # Use a positive but tiny sleep so the first worker is still
    # ``running`` when the second ``schedule_revalidation`` lands —
    # otherwise the first task finishes in microseconds and the
    # cancellation branch is never exercised.
    revalidation.STEP_SLEEP_SECONDS = 0.5
    try:
        first_id = revalidation.schedule_revalidation(store, period)
        # Give the first worker a moment to transition from
        # ``pending`` to ``running`` so the cancellation branch can
        # observe it.
        import time
        time.sleep(0.05)
        second_id = revalidation.schedule_revalidation(store, period)
        # The first task row must be marked cancelled — even if the
        # worker thread itself keeps running — and only the second
        # task row advances to ``done``.
        with store.connect() as db:
            row = db.execute(
                "SELECT status FROM revalidation_tasks WHERE id = ?",
                (first_id,),
            ).fetchone()
        assert row["status"] == "cancelled"
        # ``second_id`` must be different — a fresh task id, not the
        # recycled first one.
        assert second_id != first_id
        assert revalidation.wait_for_task(second_id, timeout=10)
    finally:
        revalidation.STEP_SLEEP_SECONDS = original_sleep

    task = store.get_revalidation_task(second_id)
    assert task is not None
    assert task["status"] == "done"
    assert task["summary"]["recalculated"] == 3
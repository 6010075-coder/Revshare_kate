"""Step 3 JSON status endpoint for the revalidation progress widget.

The endpoint ``/admin/reconciliation-base/revalidation-status/{task_id}``
returns the live progress JSON that the in-page polling widget reads
every 400 ms. These tests verify the contract:

* Known task id → 200 + payload shape (``status``, ``total``,
  ``processed``, ``current_report``, ``summary``, ``started_at``,
  ``finished_at``).
* Unknown task id → 404.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from revshare.store import Store
from revshare.web import create_app


def _insert_task(
    store: Store,
    *,
    task_id: str,
    period: str,
    status: str,
    total: int = 0,
    processed: int = 0,
    current_report: str = "",
    started_at: str = "",
    finished_at: str = "",
    summary: str = "{}",
) -> None:
    """Insert a revalidation_tasks row directly (bypassing the worker)."""
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO revalidation_tasks
            (id, period, total, processed, current_report, status,
             started_at, finished_at, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                period,
                total,
                processed,
                current_report,
                status,
                started_at,
                finished_at,
                summary,
            ),
        )


def test_status_endpoint_returns_progress_json(tmp_path) -> None:
    """Known task id → 200 + the expected payload shape."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    task_id = "deadbeefcafef00d"
    _insert_task(
        store,
        task_id=task_id,
        period="2026-08",
        status="running",
        total=3,
        processed=2,
        current_report="Demo Integration Hub App (report 7)",
        started_at="2026-09-01T10:00:00+00:00",
        finished_at="",
        summary='{"recalculated": 2, "status_changed": 1, "errors": 0, "total": 3}',
    )

    response = client.get(f"/admin/reconciliation-base/revalidation-status/{task_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["period"] == "2026-08"
    assert payload["status"] == "running"
    assert payload["total"] == 3
    assert payload["processed"] == 2
    assert payload["current_report"] == "Demo Integration Hub App (report 7)"
    assert payload["started_at"] == "2026-09-01T10:00:00+00:00"
    assert payload["finished_at"] == ""
    assert payload["summary"] == {
        "recalculated": 2,
        "status_changed": 1,
        "errors": 0,
        "total": 3,
    }


def test_status_endpoint_404_for_unknown_task(tmp_path) -> None:
    """Unknown task id → 404."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    response = client.get(
        "/admin/reconciliation-base/revalidation-status/does-not-exist"
    )
    assert response.status_code == 404


def test_status_endpoint_handles_done_state(tmp_path) -> None:
    """A ``done`` task returns ``finished_at`` and the cached summary."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    task_id = "1234567890abcdef"
    _insert_task(
        store,
        task_id=task_id,
        period="2026-09",
        status="done",
        total=5,
        processed=5,
        current_report="",
        started_at="2026-09-01T11:00:00+00:00",
        finished_at="2026-09-01T11:00:05+00:00",
        summary='{"recalculated": 5, "status_changed": 2, "errors": 0, "total": 5}',
    )

    response = client.get(f"/admin/reconciliation-base/revalidation-status/{task_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "done"
    assert payload["processed"] == 5
    assert payload["finished_at"] == "2026-09-01T11:00:05+00:00"
    assert payload["summary"]["status_changed"] == 2
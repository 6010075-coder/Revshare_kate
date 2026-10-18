"""Step 3 revalidation progress widget in ``admin_base_upload.html``.

The upload page renders a live progress widget only while a revalidation
task is in flight for the period. The widget carries the task id in a
``data-task-id`` attribute so the inline script can poll the JSON
endpoint.

These tests render the template via the project's TestClient and verify
the contract:

* Period with a running task → widget present with the right task id.
* Period with NO active task → widget absent.
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
    status: str = "running",
) -> None:
    """Insert a minimal ``revalidation_tasks`` row for ``period``."""
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO revalidation_tasks
            (id, period, total, processed, current_report, status,
             started_at, finished_at, summary)
            VALUES (?, ?, 0, 0, '', ?, '', '', '{}')
            """,
            (task_id, period, status),
        )


def test_progress_widget_renders_when_task_running(tmp_path) -> None:
    """A running task renders the widget with the expected ``data-task-id``."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    task_id = "feedfacefeedface"
    _insert_task(store, task_id=task_id, period="2026-08", status="running")

    response = client.get("/admin/reconciliation-base/2026-08")
    assert response.status_code == 200
    body = response.text

    # The widget container is rendered with the task id in the
    # ``data-task-id`` attribute (so the inline script can poll the
    # status endpoint) and a class that the widget styling targets.
    assert 'id="revalidation-progress"' in body
    assert f'data-task-id="{task_id}"' in body
    # The polling URL is rendered with the task id too — confirms the
    # widget is fully wired up rather than partially rendered.
    assert (
        f"/admin/reconciliation-base/revalidation-status/{task_id}" in body
    )


def test_progress_widget_absent_when_no_active_task(tmp_path) -> None:
    """No running task → the widget must NOT be rendered."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    response = client.get("/admin/reconciliation-base/2026-08")
    assert response.status_code == 200
    body = response.text

    # The widget container id must not be present at all when there is
    # no active revalidation task for the period.
    assert 'id="revalidation-progress"' not in body
    assert "data-task-id=" not in body
    # The page still renders the upload form.
    assert 'name="base_file"' in body


def test_progress_widget_renders_only_for_matching_period(tmp_path) -> None:
    """A task for ``2026-08`` does NOT leak into the ``2026-09`` page."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    task_id = "aaaa1111bbbb2222"
    _insert_task(store, task_id=task_id, period="2026-08", status="running")

    other = client.get("/admin/reconciliation-base/2026-09")
    assert other.status_code == 200
    assert 'id="revalidation-progress"' not in other.text
    assert task_id not in other.text
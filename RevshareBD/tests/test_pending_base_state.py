"""Step 3 partner-facing flow: ``pending_base`` review screen.

The partner-facing review endpoint at ``/submit/{token}/review/{draft}``
must switch to the "waiting for base" branch when no reconciliation
base has been uploaded for the period yet, and the "Confirm" submit
button must NOT be rendered in that branch. When a base IS uploaded
the review screen falls back to the usual discrepancies / Confirm
layout.

These tests exercise the full HTTP flow with the project's test client
and the same ``_seed_reconciliation_base`` helper that ``test_web.py``
uses, so they cover both the happy path and the ``pending_base`` branch
end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from revshare.extract import BaseRow
from revshare.store import Store
from revshare.web import create_app


def _seed_base(
    store: Store,
    period: str,
    *,
    connected_ids: list[str],
    active_ids: list[str],
) -> None:
    """Seed a reconciliation base that mirrors the legacy ``replace_usage``."""
    rows: list[BaseRow] = []
    seen: set[str] = set()
    for location_id in connected_ids:
        if location_id in seen:
            continue
        seen.add(location_id)
        rows.append(
            BaseRow(
                application_id=1,
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


def _parse_registry(
    client: TestClient,
    period: str,
    csv_body: str,
    tmp_path: Path,
) -> str:
    """Post the partner file and return the resulting ``draft_token``."""
    registry = tmp_path / "registry.csv"
    registry.write_text(csv_body, encoding="utf-8")
    with registry.open("rb") as handle:
        preview = client.post(
            "/submit/demo-partner/parse",
            data={"period": period},
            files={"attachment": ("registry.csv", handle, "text/csv")},
        )
    assert preview.status_code == 200
    return str(preview.url).rstrip("/").split("/")[-1]


def test_review_screen_without_base_shows_waiting_branch(tmp_path: Path) -> None:
    """No base → "Waiting for reconciliation base" branch, no Confirm button."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    # Seed an empty usage import so the partner-flow does not redirect
    # to the snapshot_missing error before reaching the review screen.
    partner = store.partner_by_token("demo-partner")
    store.replace_usage(
        partner["id"],
        "2026-08",
        connected_ids=["101"],
        active_ids=["101"],
    )

    draft_token = _parse_registry(
        client,
        "2026-08",
        "Location ID,Turnover\n101,$400\n",
        tmp_path,
    )

    response = client.get(f"/submit/demo-partner/review/{draft_token}")
    assert response.status_code == 200
    body = response.text

    # The waiting-for-base branch is rendered with the resolved i18n text.
    assert "Waiting for reconciliation base" in body
    # The translated "base not loaded" message (with the period
    # interpolated) is rendered to the partner.
    assert "The reconciliation base for 2026-08 has not been uploaded" in body
    # The form that contains the Confirm submit button must NOT be
    # rendered — the partner is supposed to wait for the admin.
    assert "/submit/demo-partner/confirm/" not in body
    assert ">Submit report<" not in body
    assert ">Submit report with differences<" not in body
    # The "upload another" link is rendered so the partner can still
    # resubmit a corrected file later.
    assert "upload_another" in body or "Upload another" in body or "Replace file" in body


def test_review_screen_with_base_shows_confirm_button(tmp_path: Path) -> None:
    """Base present → review screen renders the assess result + Confirm."""
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")

    store.replace_usage(
        partner["id"],
        "2026-08",
        connected_ids=["101"],
        active_ids=["101"],
    )
    _seed_base(
        store,
        "2026-08",
        connected_ids=["101"],
        active_ids=["101"],
    )

    draft_token = _parse_registry(
        client,
        "2026-08",
        "Location ID,Turnover\n101,$400\n",
        tmp_path,
    )

    response = client.get(f"/submit/demo-partner/review/{draft_token}")
    assert response.status_code == 200
    body = response.text

    # Confirm button IS rendered once a base is available.
    assert f"/submit/demo-partner/confirm/{draft_token}" in body
    assert "Confirm submit" in body or "Submit report" in body
    # The waiting-for-base message must NOT appear when a base exists.
    assert "Waiting for reconciliation base" not in body
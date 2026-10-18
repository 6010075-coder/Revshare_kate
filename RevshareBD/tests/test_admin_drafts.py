"""Admin draft management: list drafts, delete a draft, partner ownership guard.

Note: CSRF middleware is disabled in tests because they pass a temporary
database path (see ``require_admin_auth`` in web.py). The middleware logic
itself is exercised by ``tests/test_web_csrf.py``-style coverage elsewhere
in the suite; here we focus on the business logic — draft listing and
cross-partner access protection.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from revshare.store import Store
from revshare.web import create_app


def _create_draft(store: Store, partner_id: int, period: str = "2026-08") -> int:
    return store.save_bot_draft(
        partner_id=partner_id,
        period=period,
        application_id=None,
        attachment=f"{partner_id}_{period}_seed.xlsx",
        turnover=100.0,
        currency="USD",
        reported_share=20.0,
        location_ids=["loc-1"],
        extraction_notes=[],
        application_breakdown=[],
    )


def test_admin_can_delete_bot_draft(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")

    draft_id = _create_draft(store, partner["id"])
    assert [d["id"] for d in store.drafts_for_partner(partner["id"])] == [draft_id]

    response = client.post(
        f"/admin/partners/{partner['id']}/drafts/{draft_id}/delete",
        data={"csrf_token": app.state.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.drafts_for_partner(partner["id"]) == []


def test_admin_drafts_view_renders_rows_and_delete_button(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")
    _create_draft(store, partner["id"], period="2026-07")
    _create_draft(store, partner["id"], period="2026-08")

    page = client.get(f"/admin/partners/{partner['id']}/edit?tab=history&view=drafts")
    assert page.status_code == 200
    assert "August 2026" in page.text
    assert "July 2026" in page.text
    # Two delete forms (one per draft), each posting to the right endpoint.
    assert page.text.count("/drafts/") >= 2
    # Localised confirm prompt is rendered into the form's data-confirm.
    assert "draft" in page.text and "restore" in page.text.lower()


def test_delete_draft_from_other_partner_is_404(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    owner = store.partner_by_token("demo-partner")
    intruder_id = store.add_partner(
        name="Other Partner",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        token="other-partner-token",
        rate=0.2,
        currency="USD",
    )

    draft_id = _create_draft(store, owner["id"])
    response = client.post(
        f"/admin/partners/{intruder_id}/drafts/{draft_id}/delete",
        data={"csrf_token": app.state.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404
    # Original draft is untouched.
    assert [d["id"] for d in store.drafts_for_partner(owner["id"])] == [draft_id]


def test_delete_unknown_draft_is_404(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")

    response = client.post(
        f"/admin/partners/{partner['id']}/drafts/999999/delete",
        data={"csrf_token": app.state.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_default_history_view_still_shows_reports_not_drafts(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")
    _create_draft(store, partner["id"])

    # Without ?view=drafts the page must NOT show the drafts table.
    page = client.get(f"/admin/partners/{partner['id']}/edit?tab=history")
    assert page.status_code == 200
    # Drafts toggle label is NOT visible on the default view.
    assert "Черновики" not in page.text
    # Reports table heading is still there.
    assert "Reported months" in page.text or "No reports yet" in page.text


def test_delete_unknown_partner_is_404(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    response = client.post(
        "/admin/partners/999999/drafts/1/delete",
        data={"csrf_token": app.state.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404
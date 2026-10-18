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


def test_admin_cannot_delete_bot_draft(tmp_path):
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
    assert response.status_code == 404
    assert [d["id"] for d in store.drafts_for_partner(partner["id"])] == [draft_id]


def test_admin_drafts_view_renders_rows_and_download_links(tmp_path):
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
    assert page.text.count("/drafts/") >= 1
    assert "/delete" not in page.text
    assert "Download file" in page.text


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
    # The original draft is untouched and the admin action is not available.
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


def test_history_is_grouped_by_period_and_includes_draft(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")
    _create_draft(store, partner["id"])

    # The history is no longer split into report/draft tabs.
    page = client.get(f"/admin/partners/{partner['id']}/edit?tab=history")
    assert page.status_code == 200
    assert "Черновики" not in page.text
    assert "August 2026" in page.text
    assert "File uploaded" in page.text
    assert "_2026-08_seed.xlsx" in page.text


def test_delete_unknown_partner_is_404(tmp_path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    response = client.post(
        "/admin/partners/999999/drafts/1/delete",
        data={"csrf_token": app.state.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404

def test_partner_report_navigation_and_unified_period_history(tmp_path):
    from revshare.web import create_app
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / 'report-ux.db')
    client = TestClient(app)
    store = app.state.store
    partner = store.partner_by_token('demo-partner')
    partner_id = partner['id']
    report_id, _ = store.submit_report(partner_id=partner_id, period='2026-08', turnover=100, currency='USD', reported_share=20, location_ids=['1'], comment='', attachment=None)
    store.submit_report(partner_id=partner_id, period='2026-08', turnover=200, currency='USD', reported_share=40, location_ids=['1'], comment='', attachment=None)
    store.save_bot_draft(partner_id=partner_id, period='2026-08', application_id=None, attachment='draft.xlsx', turnover=50, currency='USD', reported_share=10, location_ids=['1'], extraction_notes=[], application_breakdown=[])

    profile = client.get(f'/admin/partners/{partner_id}/edit?tab=history&view=reports')
    assert profile.status_code == 200
    assert 'Черновики' not in profile.text
    assert 'August 2026' in profile.text
    assert 'draft.xlsx' in profile.text
    assert 'File uploaded' in profile.text
    assert f'/admin/partners/{partner_id}/drafts/' in profile.text
    period_page = client.get(f'/admin/partners/{partner_id}/edit?tab=history&period=2026-08')
    assert 'Version 2' in period_page.text
    assert 'File uploaded' in period_page.text
    report_page = client.get(f'/reports/{report_id}')
    assert report_page.status_code == 200
    assert 'Submitted versions' in report_page.text
    assert 'Activity' in report_page.text
    assert 'Version 2' in report_page.text and 'Version 1' in report_page.text
    assert '200.00 USD' in report_page.text and '100.00 USD' in report_page.text
    assert f'/admin/partners/{partner_id}/edit?tab=history&amp;period=2026-08' in report_page.text
    assert 'draft.xlsx' not in report_page.text

"""Acceptance tests for the simplified dashboard statuses (ТЗ «сводка»).

Four agreed statuses are exposed by the summary:
* ``missing``            — no saved report for the period.
* ``partner_submitted``  — partner uploaded via the bot/site, admin not yet confirmed.
* ``admin_submitted``    — admin entered manually, admin not yet confirmed.
* ``confirmed``          — admin pressed "Confirm" (covers ``posted`` too).

The acceptance scenarios below cover the happy paths and the key
"recheck must not drop confirmation" / "resubmit resets confirmation"
invariants from the task spec.
"""

from __future__ import annotations

from revshare.web import DASHBOARD_STATUSES, create_app


def _dashboard_item(store, partner_id, period="2026-08"):
    return next(item for item in store.dashboard(period) if item["id"] == partner_id)


def _submit_via_bot(store, partner_id, period="2026-08"):
    return store.submit_report(
        partner_id=partner_id,
        period=period,
        turnover=100,
        currency="USD",
        reported_share=30,
        location_ids=["101"],
        comment="",
        attachment=None,
        report_source="partner_file_bot",
    )[0]


def test_missing_when_no_report(tmp_path):
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    assert _dashboard_item(store, partner)["display_status"] == "missing"


def test_partner_submitted_after_bot_upload(tmp_path):
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    store.replace_usage(partner, "2026-08", connected_ids=[], active_ids=[])
    _submit_via_bot(store, partner)
    assert _dashboard_item(store, partner)["display_status"] == "partner_submitted"


def test_admin_submitted_after_manual_entry(tmp_path):
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    store.submit_manual_report(partner, "2026-08", 100, "USD", 30, "test")
    assert _dashboard_item(store, partner)["display_status"] == "admin_submitted"


def test_confirmed_after_admin_approve(tmp_path):
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    report_id = _submit_via_bot(store, partner)
    store.approve_report(report_id)
    assert _dashboard_item(store, partner)["display_status"] == "confirmed"


def test_recheck_does_not_drop_confirmation(tmp_path):
    """An automatic recheck must not silently undo an admin confirmation."""
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    report_id = _submit_via_bot(store, partner)
    store.approve_report(report_id)
    # Trigger an automatic recheck via the same path the revalidation worker uses.
    store.replace_usage(partner, "2026-08", connected_ids=[], active_ids=[])
    assert _dashboard_item(store, partner)["display_status"] == "confirmed"
    report = store.report(report_id)
    assert report["workflow_status"] in ("approved", "posted")


def test_resubmit_resets_confirmation_to_submission_type(tmp_path):
    """A fresh partner submission clears the prior confirmation."""
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    report_id = _submit_via_bot(store, partner)
    store.approve_report(report_id)
    _submit_via_bot(store, partner)
    assert _dashboard_item(store, partner)["display_status"] == "partner_submitted"


def test_unapprove_falls_back_to_submission_type(tmp_path):
    """Explicit cancellation of confirmation returns the report to its
    submission-type status (here: partner-submitted, since the report
    came in via the bot)."""
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    report_id = _submit_via_bot(store, partner)
    store.approve_report(report_id)
    store.unapprove_report(report_id)
    assert _dashboard_item(store, partner)["display_status"] == "partner_submitted"


def test_posted_still_shows_confirmed(tmp_path):
    """Queuing a confirmed report for sheet-write must not introduce a new
    visible status — it remains "Confirmed"."""
    store = create_app(tmp_path / "test.db").state.store
    partner = store.partner_by_token("demo-partner")["id"]
    report_id = _submit_via_bot(store, partner)
    store.approve_report(report_id)
    store.mark_posted(report_id)
    assert _dashboard_item(store, partner)["display_status"] == "confirmed"


def test_dashboard_filter_has_only_four_statuses():
    """Only the four agreed statuses (plus 'all') are exposed in the filter."""
    assert DASHBOARD_STATUSES == (
        "all",
        "missing",
        "partner_submitted",
        "admin_submitted",
        "confirmed",
    )


def test_partner_submission_does_not_touch_other_partner_status(tmp_path):
    """A new submission for partner B must not change the visible status or
    its timestamp for partner A."""
    store = create_app(tmp_path / "test.db").state.store
    first = store.partner_by_token("demo-partner")["id"]
    second = store.add_partner(
        name="Second",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="second@example.com",
        telegram="",
        report_channel="email",
        token="second-token",
        rate=0.3,
        currency="USD",
    )
    first_report = _submit_via_bot(store, first)
    old = "2020-01-01T00:00:00+00:00"
    with store.connect() as db:
        db.execute(
            "UPDATE reports SET status_updated_at = ? WHERE id = ?",
            (old, first_report),
        )
    _submit_via_bot(store, second)
    assert store.report(first_report)["status_updated_at"] == old

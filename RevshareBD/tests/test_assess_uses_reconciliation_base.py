"""Step 3 of the partner pivot: ``assess`` must read from the new base.

These tests exercise the Store methods that back the partner-facing
``assess`` call (``get_reconciliation_base_rows``,
``get_report_clients``, ``list_reports_for_period``,
``update_report_assessment``) and the new ``submit_report`` routing:
when no reconciliation base is present, the report lands in the
``pending_base`` state; when a base exists, ``assess`` is called with the
union of base rows for connected ids and the active subset for active ids.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from revshare.extract import BaseRow
from revshare.store import Store
from revshare.web import create_app


def _seed_base(
    store: Store,
    period: str,
    *,
    rows: list[BaseRow],
) -> None:
    store.replace_reconciliation_base(
        period=period,
        uploaded_at="2026-09-01T00:00:00+00:00",
        uploaded_by="tests",
        file_path="tests/base.csv",
        rows=rows,
        notes="",
    )


def test_get_reconciliation_base_rows_returns_dicts_with_expected_keys(
    tmp_path,
) -> None:
    store = Store(tmp_path / "base.db")
    _seed_base(
        store,
        "2026-08",
        rows=[
            BaseRow(101, "1001", 100.0, "USD", True, 1),
            BaseRow(101, "1002", 50.0, "USD", True, 2),
            BaseRow(101, "1003", 0.0, "USD", False, 3),
        ],
    )

    rows = store.get_reconciliation_base_rows("2026-08")

    assert [row["location_id"] for row in rows] == ["1001", "1002", "1003"]
    for row in rows:
        assert set(row.keys()) >= {
            "application_id",
            "location_id",
            "turnover",
            "currency",
            "active",
        }
    active_flags = {row["location_id"]: bool(row["active"]) for row in rows}
    assert active_flags == {"1001": True, "1002": True, "1003": False}


def test_get_reconciliation_base_rows_is_empty_when_no_base(tmp_path) -> None:
    store = Store(tmp_path / "base.db")

    assert store.get_reconciliation_base_rows("2026-09") == []
    # Empty result must NOT be confused with "base exists but empty rows".
    assert store.get_reconciliation_base("2026-09") is None


def test_get_reconciliation_base_rows_for_empty_base_returns_empty_list(
    tmp_path,
) -> None:
    """A base metadata row with zero rows must still appear as existing."""
    store = Store(tmp_path / "base.db")
    _seed_base(store, "2026-08", rows=[])

    assert store.get_reconciliation_base("2026-08") is not None
    assert store.get_reconciliation_base_rows("2026-08") == []


def test_list_reports_for_period_returns_report_ids_ordered(tmp_path) -> None:
    store = Store(tmp_path / "reports.db")
    with store.connect() as db:
        for partner_id in (1, 2, 3):
            db.execute(
                """
                INSERT INTO partners
                (name, token, rate, currency)
                VALUES (?, ?, ?, ?)
                """,
                (f"partner-{partner_id}", f"token-{partner_id}", 0.3, "USD"),
            )
        # Insert reports in non-monotonic order to verify the
        # ORDER BY id ASC contract.
        for partner_id, period in (
            (2, "2026-08"),
            (1, "2026-08"),
            (3, "2026-08"),
            (1, "2026-09"),
        ):
            db.execute(
                """
                INSERT INTO reports
                (partner_id, period, turnover, currency, client_count, status,
                 risk_level, risk_score, reasons, missing_active, unknown_locations,
                 submitted_at, report_source)
                VALUES (?, ?, 0, 'USD', 0, 'verified', 'green', 0, '[]', '[]', '[]',
                        '2026-08-31T00:00:00+00:00', 'partner_file')
                """,
                (partner_id, period),
            )

    assert store.list_reports_for_period("2026-08") == [1, 2, 3]
    assert store.list_reports_for_period("2026-09") == [4]
    assert store.list_reports_for_period("2099-01") == []


def test_get_report_clients_returns_strings_in_order(tmp_path) -> None:
    store = Store(tmp_path / "clients.db")
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO partners (name, token, rate, currency)
            VALUES ('p', 't', 0.3, 'USD')
            """
        )
        db.execute(
            """
            INSERT INTO reports
            (partner_id, period, turnover, currency, client_count, status,
             risk_level, risk_score, reasons, missing_active, unknown_locations,
             submitted_at, report_source)
            VALUES (1, '2026-08', 0, 'USD', 0, 'verified', 'green', 0, '[]',
                    '[]', '[]', '2026-08-31T00:00:00+00:00', 'partner_file')
            """
        )
        # Insert clients out of order; the helper must sort ascending.
        for location_id in ("x", "a", "m"):
            db.execute(
                "INSERT INTO report_clients (report_id, location_id) VALUES (?, ?)",
                (1, location_id),
            )

    assert store.get_report_clients(1) == ["a", "m", "x"]
    assert store.get_report_clients(999) == []


def test_update_report_assessment_persists_new_assess_fields(tmp_path) -> None:
    store = Store(tmp_path / "assess.db")
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO partners (name, token, rate, currency)
            VALUES ('p', 't', 0.3, 'USD')
            """
        )
        db.execute(
            """
            INSERT INTO reports
            (partner_id, period, turnover, currency, client_count, status,
             risk_level, risk_score, reasons, missing_active, unknown_locations,
             submitted_at, report_source)
            VALUES (1, '2026-08', 1000, 'USD', 1, 'verified', 'green', 0, '[]',
                    '[]', '[]', '2026-08-31T00:00:00+00:00', 'partner_file')
            """
        )

    store.update_report_assessment(
        report_id=1,
        level="red",
        score=87,
        reasons_json='[{"key": "reason_missing_active", "count": 3}]',
        missing_active_json='["abc", "def", "ghi"]',
        unknown_locations_json='["xyz"]',
    )

    with store.connect() as db:
        row = db.execute(
            """
            SELECT risk_level, risk_score, reasons, missing_active,
                   unknown_locations
            FROM reports WHERE id = 1
            """
        ).fetchone()
    assert row["risk_level"] == "red"
    assert row["risk_score"] == 87
    assert "reason_missing_active" in row["reasons"]
    assert "abc" in row["missing_active"]
    assert "xyz" in row["unknown_locations"]


def test_partner_submit_with_base_calls_assess_with_base_ids(tmp_path) -> None:
    """End-to-end: with a base present, ``submit_report`` must call
    ``assess`` against the base's connected/active sets (not the legacy
    usage snapshot).
    """
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")

    # Seed a base that EXCLUDES the partner's reported location — this
    # forces ``assess`` to flag an "unknown" id and raise the risk score.
    _seed_base(
        store,
        "2026-08",
        rows=[
            BaseRow(101, "999", 0.0, "USD", True, 1),
            BaseRow(101, "888", 0.0, "USD", False, 2),
        ],
    )

    report_id, review = store.submit_report(
        partner_id=partner["id"],
        period="2026-08",
        turnover=1000,
        currency="USD",
        reported_share=300,
        location_ids=["101"],  # not in the base → unknown
        comment="",
        attachment=None,
    )

    with store.connect() as db:
        row = db.execute(
            "SELECT status FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
    assert row["status"] == "requires_review"
    assert review.unknown == ["101"]
    assert "101" not in review.missing_active  # 101 is unknown, not missing


def test_partner_submit_without_base_lands_in_pending_base(tmp_path) -> None:
    """No base uploaded for the period → report is parked as
    ``pending_base`` with empty assessment fields.
    """
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store
    partner = store.partner_by_token("demo-partner")

    report_id, review = store.submit_report(
        partner_id=partner["id"],
        period="2026-08",
        turnover=1000,
        currency="USD",
        reported_share=300,
        location_ids=["101"],
        comment="",
        attachment=None,
    )

    with store.connect() as db:
        row = db.execute(
            """
            SELECT status, risk_level, risk_score, reasons,
                   missing_active, unknown_locations
            FROM reports WHERE id = ?
            """,
            (report_id,),
        ).fetchone()
    assert row["status"] == "pending_base"
    # Empty assessment fields: the admin will recompute these once the
    # base arrives and the revalidation worker runs.
    assert row["risk_level"] == "green"
    assert row["risk_score"] == 0
    assert json.loads(row["reasons"]) == []
    assert json.loads(row["missing_active"]) == []
    assert json.loads(row["unknown_locations"]) == []
    # ``assess`` is short-circuited so the returned Review is empty.
    assert review.reasons == []
    assert review.missing_active == []


def test_helper_returns_correct_connected_active_sets(tmp_path) -> None:
    """The web helper that drives the ``assess`` call must expose the
    right id sets and report whether a base exists.
    """
    app = create_app(tmp_path / "test.db")
    store: Store = app.state.store

    _seed_base(
        store,
        "2026-08",
        rows=[
            BaseRow(101, "101", 0.0, "USD", True, 1),
            BaseRow(101, "102", 0.0, "USD", True, 2),
            BaseRow(101, "103", 0.0, "USD", False, 3),
        ],
    )

    from revshare.web import _load_connected_active_ids_for_test

    connected, active, base_exists = _load_connected_active_ids_for_test(
        store, "2026-08"
    )
    assert base_exists is True
    assert connected == {"101", "102", "103"}
    assert active == {"101", "102"}

    connected, active, base_exists = _load_connected_active_ids_for_test(
        store, "2099-01"
    )
    assert base_exists is False
    assert connected == set()
    assert active == set()
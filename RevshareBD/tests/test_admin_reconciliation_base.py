"""Admin reconciliation-base upload page (step 2 of 5)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from revshare.store import Store
from revshare.web import MAX_BASE_UPLOAD_BYTES, create_app


CSV_HEADER = (
    "Application ID,Location ID,Turnover,Currency,Active\n"
    "101,12345,1000.50,USD,1\n"
    "101,12346,500,USD,true\n"
    "101,12347,250.75,USD,yes\n"
    "102,12348,0,EUR,no\n"
    "102,12349,75.25,EUR,1\n"
)


def _write_csv(path: Path) -> Path:
    path.write_text(CSV_HEADER, encoding="utf-8")
    return path


def _write_xlsx(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Application ID", "Location ID", "Turnover", "Currency", "Active"])
    for row_id in range(5):
        sheet.append([100 + row_id, 20000 + row_id, 100.0 * row_id, "USD", 1])
    workbook.save(path)
    return path


def test_get_shows_empty_state_when_no_base_exists(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    page = client.get("/admin/reconciliation-base/2026-08")

    assert page.status_code == 200
    body = page.text
    assert "No reconciliation base uploaded for this month yet." in body
    # Status labels should NOT appear when no base exists.
    assert "Rows:" not in body
    # Upload form is rendered.
    assert 'name="base_file"' in body
    assert "/admin/reconciliation-base/2026-08" in body


def test_get_shows_status_card_when_base_exists(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    csv_path = _write_csv(tmp_path / "seed.csv")
    upload_root = tmp_path / "uploads" / "reconciliation_base"
    upload_root.mkdir(parents=True, exist_ok=True)
    stored = upload_root / "2026-08-seed.csv"
    csv_path.read_bytes()
    stored.write_bytes(csv_path.read_bytes())

    store.replace_reconciliation_base(
        period="2026-08",
        uploaded_at="2026-09-01T12:00:00+00:00",
        uploaded_by="kate",
        file_path=f"reconciliation_base/{stored.name}",
        rows=[],
        notes="",
    )
    # row_count is set by replace_reconciliation_base itself; overwrite manually
    # so we exercise the metadata-only branch here.
    with store.connect() as db:
        db.execute(
            "UPDATE reconciliation_bases SET row_count = 5 WHERE period = ?",
            ("2026-08",),
        )

    page = client.get("/admin/reconciliation-base/2026-08")
    assert page.status_code == 200
    body = page.text
    assert "Uploaded:" in body
    assert "Rows:" in body
    assert "File:" in body
    assert "By:" in body
    assert "reconciliation_base/2026-08-seed.csv" in body
    # The empty-state hint must NOT be present when a base exists.
    assert "No reconciliation base uploaded for this month yet." not in body


def test_post_valid_csv_replaces_existing_rows_and_updates_metadata(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    # Seed a previous base with different rows so we can prove replacement.
    store.replace_reconciliation_base(
        period="2026-08",
        uploaded_at="2026-08-01T00:00:00+00:00",
        uploaded_by="legacy",
        file_path="reconciliation_base/old.csv",
        rows=[],
        notes="",
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO reconciliation_base_rows (period, application_id, location_id, turnover, currency, active, source_row) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-08", 999, "OLD-LOC", 1.0, "USD", 0, 1),
        )

    csv_path = _write_csv(tmp_path / "base.csv")
    with csv_path.open("rb") as handle:
        response = client.post(
            "/admin/reconciliation-base/2026-08",
            data={"csrf_token": app.state.csrf_token},
            files={"base_file": ("base.csv", handle, "text/csv")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/admin/reconciliation-base/2026-08?saved=1"
    )

    base = store.get_reconciliation_base("2026-08")
    assert base is not None
    assert base["row_count"] == 5
    assert base["uploaded_by"] == "kate"
    assert base["file_path"].startswith("reconciliation_base/2026-08-")
    assert base["file_path"].endswith(".csv")
    assert base["uploaded_at"]

    with store.connect() as db:
        rows = db.execute(
            "SELECT location_id FROM reconciliation_base_rows WHERE period = ? ORDER BY location_id",
            ("2026-08",),
        ).fetchall()
    assert [r["location_id"] for r in rows] == ["12345", "12346", "12347", "12348", "12349"]

    # Posting the same file twice must not duplicate rows.
    csv_again = _write_csv(tmp_path / "base-again.csv")
    with csv_again.open("rb") as handle:
        second = client.post(
            "/admin/reconciliation-base/2026-08",
            data={"csrf_token": app.state.csrf_token},
            files={"base_file": ("base-again.csv", handle, "text/csv")},
            follow_redirects=False,
        )
    assert second.status_code == 303
    assert store.get_reconciliation_base("2026-08")["row_count"] == 5
    with store.connect() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM reconciliation_base_rows WHERE period = ?",
            ("2026-08",),
        ).fetchone()[0]
    assert total == 5


def test_post_valid_xlsx_is_accepted(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    xlsx_path = _write_xlsx(tmp_path / "base.xlsx")
    with xlsx_path.open("rb") as handle:
        response = client.post(
            "/admin/reconciliation-base/2026-08",
            data={"csrf_token": app.state.csrf_token},
            files={"base_file": ("base.xlsx", handle, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert store.get_reconciliation_base("2026-08")["row_count"] == 5


def test_post_wrong_extension_returns_error_and_does_not_touch_db(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    response = client.post(
        "/admin/reconciliation-base/2026-08",
        data={"csrf_token": app.state.csrf_token},
        files={"base_file": ("not-a-spreadsheet.txt", BytesIO(b"hello"), "text/plain")},
    )

    assert response.status_code == 400
    assert store.get_reconciliation_base("2026-08") is None
    with store.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM reconciliation_base_rows WHERE period = ?",
            ("2026-08",),
        ).fetchone()[0]
    assert count == 0


def test_post_oversized_file_returns_error(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)
    store: Store = app.state.store

    big_payload = b"a" * (MAX_BASE_UPLOAD_BYTES + 1024)
    response = client.post(
        "/admin/reconciliation-base/2026-08",
        data={"csrf_token": app.state.csrf_token},
        files={"base_file": ("big.csv", BytesIO(big_payload), "text/csv")},
    )

    assert response.status_code == 400
    assert "10 MB" in response.text or "larger" in response.text.lower()
    assert store.get_reconciliation_base("2026-08") is None
    with store.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM reconciliation_base_rows WHERE period = ?",
            ("2026-08",),
        ).fetchone()[0]
    assert count == 0
    # The raw file must NOT have been persisted on the disk.
    upload_dir = tmp_path / "uploads" / "reconciliation_base"
    if upload_dir.exists():
        assert list(upload_dir.glob("2026-08-*.csv")) == []


def test_post_invalid_period_redirects(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    response = client.post(
        "/admin/reconciliation-base/not-a-period",
        data={"csrf_token": app.state.csrf_token},
        files={"base_file": ("base.csv", BytesIO(b"x"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code in (303, 404)
    if response.status_code == 303:
        assert "error" in response.headers["location"]


def test_get_invalid_period_redirects(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    response = client.get("/admin/reconciliation-base/not-a-period", follow_redirects=False)
    assert response.status_code in (303, 404)


def test_header_button_present_on_dashboard(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    client = TestClient(app)

    page = client.get("/?period=2026-08")
    assert page.status_code == 200
    assert "/admin/reconciliation-base/2026-08" in page.text
    assert "Upload reconciliation base" in page.text

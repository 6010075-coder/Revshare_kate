"""Tests for the Store.init() migration that adds the reconciliation base
tables used by step 2–5 (admin upload + assess).

The migration must be idempotent: calling ``Store.init()`` twice on the same
file must not error, and all three tables must exist with the columns and
constraints declared in the spec.
"""

from __future__ import annotations

from pathlib import Path

from revshare.store import Store


_NEW_TABLES = (
    "reconciliation_bases",
    "reconciliation_base_rows",
    "revalidation_tasks",
)


def _columns(store: Store, table: str) -> dict[str, str]:
    with store.connect() as db:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: row["type"] for row in rows}


def _indexes(store: Store, table: str) -> list[tuple[str, str, list[str]]]:
    """Return (index_name, origin, columns) for unique indexes on ``table``."""
    result: list[tuple[str, str, list[str]]] = []
    with store.connect() as db:
        for list_row in db.execute(f"PRAGMA index_list({table})").fetchall():
            if not list_row["unique"]:
                continue
            cols = db.execute(f"PRAGMA index_info('{list_row['name']}')").fetchall()
            result.append(
                (list_row["name"], list_row["origin"], [c["name"] for c in cols])
            )
    return result


def _non_unique_indexes(store: Store, table: str) -> list[tuple[str, list[str]]]:
    """Return (index_name, columns) for non-unique indexes on ``table``."""
    result: list[tuple[str, list[str]]] = []
    with store.connect() as db:
        for list_row in db.execute(f"PRAGMA index_list({table})").fetchall():
            if list_row["unique"]:
                continue
            cols = db.execute(f"PRAGMA index_info('{list_row['name']}')").fetchall()
            result.append((list_row["name"], [c["name"] for c in cols]))
    return result


def test_init_creates_three_new_tables(tmp_path: Path):
    store = Store(tmp_path / "migration.db")
    store.init()

    with store.connect() as db:
        present = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    for table in _NEW_TABLES:
        assert table in present, f"{table} missing after init()"


def test_reconciliation_bases_columns(tmp_path: Path):
    store = Store(tmp_path / "migration.db")

    columns = _columns(store, "reconciliation_bases")

    assert columns == {
        "period": "TEXT",
        "uploaded_at": "TEXT",
        "uploaded_by": "TEXT",
        "file_path": "TEXT",
        "row_count": "INTEGER",
        "notes": "TEXT",
    }


def test_reconciliation_base_rows_columns_and_unique(tmp_path: Path):
    store = Store(tmp_path / "migration.db")

    columns = _columns(store, "reconciliation_base_rows")

    assert columns == {
        "id": "INTEGER",
        "period": "TEXT",
        "application_id": "INTEGER",
        "location_id": "TEXT",
        "turnover": "REAL",
        "currency": "TEXT",
        "active": "INTEGER",
        "source_row": "INTEGER",
    }

    uniques = _indexes(store, "reconciliation_base_rows")
    assert ("sqlite_autoindex_reconciliation_base_rows_1", "u", [
        "period",
        "application_id",
        "location_id",
    ]) in uniques


def test_reconciliation_base_rows_has_period_index(tmp_path: Path):
    store = Store(tmp_path / "migration.db")

    indexes = _non_unique_indexes(store, "reconciliation_base_rows")

    assert ("idx_reconciliation_base_rows_period", ["period"]) in indexes


def test_revalidation_tasks_columns_and_compound_index(tmp_path: Path):
    store = Store(tmp_path / "migration.db")

    columns = _columns(store, "revalidation_tasks")

    assert columns == {
        "id": "TEXT",
        "period": "TEXT",
        "total": "INTEGER",
        "processed": "INTEGER",
        "current_report": "TEXT",
        "status": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "summary": "TEXT",
    }

    indexes = _non_unique_indexes(store, "revalidation_tasks")
    assert ("idx_revalidation_tasks_period_status", ["period", "status"]) in indexes


def test_init_is_idempotent(tmp_path: Path):
    path = tmp_path / "migration.db"
    store = Store(path)
    store.init()
    # Second init() must not raise and must leave tables intact.
    store.init()

    with store.connect() as db:
        present = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for table in _NEW_TABLES:
        assert table in present
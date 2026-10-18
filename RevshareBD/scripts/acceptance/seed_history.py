"""Подготовка временной БД и uploads для ручной приёмочной проверки истории отчёта.

Запускать из корня RevshareBD:
    .venv/bin/python scripts/acceptance/seed_history.py

Создаёт:
  /tmp/revshare-acceptance/test.db        — временная БД
  /tmp/revshare-acceptance/uploads/       — папка с xlsx-файлами версий
  Партнёр "Acceptance Test Partner" с отчётом за 2026-08 (2 версии, posted)
  Партнёр "Acceptance Manual Partner"  с ручным отчётом за 2026-08 (без файла)

События истории выставляются с фиксированными датами/временем, чтобы
порядок был детерминированным.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path("/tmp/revshare-acceptance/test.db")
# Файлы версий должны жить в data/uploads — это путь, по которому
# web.py их отдаёт через /reports/{id}/versions/{n}/download.
# Используем уникальный префикс, чтобы не перепутать с реальными файлами
# партнёров. После приёмочной проверки нужно вызвать cleanup_uploads().
UPLOADS = ROOT / "data" / "uploads"
PERIOD = "2026-08"
FILE_PREFIX = "acc_test_"

# Дата/время событий.
SUBMIT_V1_AT = datetime(2026, 9, 3, 10, 15, tzinfo=UTC).isoformat()
APPROVE_V1_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC).isoformat()
SUBMIT_V2_AT = datetime(2026, 9, 7, 9, 30, tzinfo=UTC).isoformat()
APPROVE_V2_AT = datetime(2026, 9, 7, 11, 45, tzinfo=UTC).isoformat()
POSTED_AT = datetime(2026, 9, 8, 16, 20, tzinfo=UTC).isoformat()
MANUAL_AT = datetime(2026, 9, 10, 14, 0, tzinfo=UTC).isoformat()


def make_xlsx(path: Path, turnover_total: float) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws["B3"] = PERIOD
    ws["A10"] = "Location ID"
    ws["B10"] = "Turnover"
    ws["A11"] = "101"
    ws["B11"] = turnover_total
    wb.save(path)


def cleanup_uploads() -> None:
    """Удаляет только acceptance-файлы (с FILE_PREFIX) из data/uploads."""
    if not UPLOADS.exists():
        return
    for path in UPLOADS.glob(f"{FILE_PREFIX}*"):
        path.unlink()


def seed() -> dict:
    if DB_PATH.exists():
        DB_PATH.unlink()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    # Удалить прошлые acceptance-файлы.
    cleanup_uploads()

    sys.path.insert(0, str(ROOT / ".venv" / "lib"))
    from revshare.extract import BaseRow  # type: ignore
    from revshare.store import Store

    store = Store(DB_PATH)

    # --------------------------------------------------------------
    # 1) Партнёр с двумя версиями (с файлами).
    # --------------------------------------------------------------
    p1 = store.add_partner(
        name="Acceptance Test Partner",
        legal_name="",
        legal_country="Россия",
        altegio_legal_entity="",
        contact_name="Тест",
        email="accept@example.com",
        telegram="",
        report_channel="email",
        token="accept-partner",
        rate=0.2,
        currency="USD",
    )

    store.replace_reconciliation_base(
        period=PERIOD,
        uploaded_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC).isoformat(),
        uploaded_by="acceptance",
        file_path="/tmp/fake-base.csv",
        rows=[
            BaseRow(
                application_id=1,
                location_id="101",
                turnover=1000.0,
                currency="USD",
                active=True,
                source_row=2,
            ),
            BaseRow(
                application_id=1,
                location_id="102",
                turnover=500.0,
                currency="USD",
                active=False,
                source_row=3,
            ),
        ],
    )

    # --- Версия 1 ---
    file_v1_name = f"{FILE_PREFIX}{p1}_{PERIOD}_v1.xlsx"
    file_v1_path = UPLOADS / file_v1_name
    make_xlsx(file_v1_path, turnover_total=900.0)
    rel_v1 = file_v1_name

    report_id, _ = store.submit_report(
        partner_id=p1,
        period=PERIOD,
        turnover=900.0,
        currency="USD",
        reported_share=180.0,
        location_ids=["101"],
        comment="Первая отправка",
        attachment=rel_v1,
    )
    store.approve_report(report_id, approved_by="kate")

    # --- Версия 2 ---
    file_v2_name = f"{FILE_PREFIX}{p1}_{PERIOD}_v2.xlsx"
    file_v2_path = UPLOADS / file_v2_name
    make_xlsx(file_v2_path, turnover_total=1500.0)
    rel_v2 = file_v2_name

    store.submit_report(
        partner_id=p1,
        period=PERIOD,
        turnover=1500.0,
        currency="USD",
        reported_share=300.0,
        location_ids=["101", "102"],
        comment="Исправленная отправка",
        attachment=rel_v2,
    )
    store.approve_report(report_id, approved_by="kate")
    store.mark_posted(report_id, detail="Queued by acceptance test")

    # Подменим created_at событий на фиксированные.
    with store.connect() as db:
        # submitted v1 → SUBMIT_V1_AT
        db.execute(
            """
            UPDATE report_events SET created_at = ?
            WHERE report_id = ? AND note = 'submitted'
            """,
            (SUBMIT_V1_AT, report_id),
        )
        # approved v1 → APPROVE_V1_AT (раньше чем approved v2)
        db.execute(
            """
            UPDATE report_events SET created_at = ?
            WHERE report_id = ? AND note = 'approved'
              AND report_version_id = (SELECT id FROM report_versions
                                       WHERE report_id = ? AND version = 1)
            """,
            (APPROVE_V1_AT, report_id, report_id),
        )
        # resubmitted v2 → SUBMIT_V2_AT
        db.execute(
            """
            UPDATE report_events SET created_at = ?
            WHERE report_id = ? AND note = 'resubmitted'
            """,
            (SUBMIT_V2_AT, report_id),
        )
        # approved v2 → APPROVE_V2_AT
        db.execute(
            """
            UPDATE report_events SET created_at = ?
            WHERE report_id = ? AND note = 'approved'
              AND report_version_id = (SELECT id FROM report_versions
                                       WHERE report_id = ? AND version = 2)
            """,
            (APPROVE_V2_AT, report_id, report_id),
        )
        # queued → POSTED_AT
        db.execute(
            """
            UPDATE report_events SET created_at = ?
            WHERE report_id = ? AND note = 'queued'
            """,
            (POSTED_AT, report_id),
        )
        # posted_at в reports
        db.execute(
            "UPDATE reports SET posted_at = ? WHERE id = ?",
            (POSTED_AT, report_id),
        )

    # --------------------------------------------------------------
    # 2) Партнёр с ручным отчётом (без файла).
    # --------------------------------------------------------------
    p2 = store.add_partner(
        name="Acceptance Manual Partner",
        legal_name="",
        legal_country="Армения",
        altegio_legal_entity="",
        contact_name="Тест2",
        email="manual@example.com",
        telegram="",
        report_channel="email",
        token="accept-manual",
        rate=0.15,
        currency="AMD",
    )
    report_id_manual, _ = store.submit_report(
        partner_id=p2,
        period=PERIOD,
        turnover=250000.0,
        currency="AMD",
        reported_share=37500.0,
        location_ids=["201", "202"],
        comment="Ручной ввод",
        attachment=None,
        report_source="manual",
        event_workflow_status="manual",
        event_note="manual",
        event_actor="admin",
    )
    with store.connect() as db:
        db.execute(
            """
            UPDATE report_events SET created_at = ?
            WHERE report_id = ? AND note = 'manual'
            """,
            (MANUAL_AT, report_id_manual),
        )

    return {
        "db_path": str(DB_PATH),
        "uploads": str(UPLOADS),
        "report_id_v1": report_id,
        "report_id_manual": report_id_manual,
        "partner_id_p1": p1,
        "partner_id_p2": p2,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true", help="Удалить acceptance-файлы и БД")
    args = parser.parse_args()
    if args.cleanup:
        cleanup_uploads()
        if DB_PATH.exists():
            DB_PATH.unlink()
        print("cleaned up")
    else:
        info = seed()
        for k, v in info.items():
            print(f"{k}: {v}")
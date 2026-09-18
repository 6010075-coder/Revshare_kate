from datetime import datetime
from unittest.mock import MagicMock, patch

from revshare.altegio_sync import (
    application_ids_for_partner,
    parse_application_id,
    sync_period,
)
from revshare.store import Store


def test_parse_application_id_from_hub_url():
    assert parse_application_id("https://app.alteg.io/app/564") == 564
    assert parse_application_id("https://app.alteg.io/app/7/") == 7
    assert parse_application_id("https://app.alteg.io/e/mp_38_assist/") == 38
    assert parse_application_id("https://app.alteg.io/e/mp_564_ai_beauty_bot/") == 564
    assert parse_application_id("https://example.com/other") is None


def test_application_ids_for_partner_reads_card_links(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="AI Beauty Bot",
        legal_name="",
        legal_country="",
        altegio_legal_entity="Open HC",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        token="beauty-bot",
        rate=0.3,
        currency="USD",
    )
    store.replace_application_links(
        partner_id,
        [{"url": "https://app.alteg.io/app/564", "comment": "", "category": ""}],
    )
    assert application_ids_for_partner(store, partner_id) == [564]


def test_sync_period_writes_usage_from_mysql(tmp_path):
    store = Store(tmp_path / "test.db")
    partner_id = store.add_partner(
        name="AI Beauty Bot",
        legal_name="",
        legal_country="",
        altegio_legal_entity="Open HC",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        token="beauty-bot",
        rate=0.3,
        currency="USD",
    )
    store.replace_application_links(
        partner_id,
        [{"url": "https://app.alteg.io/app/564", "comment": "", "category": ""}],
    )

    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [
            {
                "salon_id": 101,
                "connected_at": datetime(2026, 1, 5, 10, 0, 0),
                "salon_name": "Demo Salon",
            }
        ],
        [{"salon_id": 101, "last_activity_at": datetime(2026, 8, 12, 9, 0, 0)}],
        [],
        [{"salon_id": 101, "last_payment_at": datetime(2026, 8, 20, 12, 0, 0)}],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor

    with (
        patch("revshare.altegio_sync.mysql_configured", return_value=True),
        patch("revshare.altegio_sync.mysql_connection") as mysql_connection,
    ):
        mysql_connection.return_value.__enter__.return_value = connection
        result = sync_period(store, "2026-08")

    assert result.partners_synced == 1
    connected, active = store.usage(partner_id, "2026-08")
    assert connected == {"101"}
    assert active == {"101"}
    evidence = store.usage_import(partner_id, "2026-08")
    assert evidence["source"] == "altegio_bi"
    assert evidence["payment_count"] == 1

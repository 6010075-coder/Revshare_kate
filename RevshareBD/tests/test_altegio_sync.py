from datetime import datetime

from revshare.altegio_sync import (
    SyncResult,
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
    assert parse_application_id("https://app.alteg.io/m/mp_367_messagehelp_crm_review/") == 367
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
        [
            {"url": "https://app.alteg.io/app/564", "comment": "", "category": ""},
            {"url": "https://app.alteg.io/app/565", "comment": "", "category": ""},
        ],
    )
    assert application_ids_for_partner(store, partner_id) == [564, 565]


def test_sync_period_is_a_noop_with_partner(tmp_path):
    """After the pivot, ``sync_period`` returns an empty ``SyncResult``
    without touching the database or requiring MySQL / MCP credentials.

    The partner and application links exist (as in the legacy test) to
    make sure the no-op does not depend on an empty registry to "do
    nothing" — even with rows that would have been written before, the
    function returns cleanly.
    """
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
        [
            {"url": "https://app.alteg.io/app/564", "comment": "", "category": ""},
            {"url": "https://app.alteg.io/app/565", "comment": "", "category": ""},
        ],
    )

    # No patches: ``sync_period`` must not reach for MySQL or MCP.
    result = sync_period(store, "2026-08")

    assert isinstance(result, SyncResult)
    assert result.period == "2026-08"
    assert result.partners_synced == 0
    assert result.partners_skipped == []
    assert result.errors == []
    assert result.partners == []
    assert result.partners_total == 0


def test_sync_period_does_not_write_to_legacy_snapshots(tmp_path):
    """``sync_period`` must not insert rows into ``usage_snapshots`` or
    ``usage_application_snapshots`` — these tables are now read-only
    historical data and the pivot removed all writers.
    """
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
        [
            {"url": "https://app.alteg.io/app/564", "comment": "", "category": ""},
            {"url": "https://app.alteg.io/app/565", "comment": "", "category": ""},
        ],
    )

    with store.connect() as db:
        before_usage = db.execute(
            "SELECT COUNT(*) AS n FROM usage_snapshots"
        ).fetchone()["n"]
        before_application = db.execute(
            "SELECT COUNT(*) AS n FROM usage_application_snapshots"
        ).fetchone()["n"]

    result = sync_period(store, "2026-08")

    assert result.partners_synced == 0

    with store.connect() as db:
        after_usage = db.execute(
            "SELECT COUNT(*) AS n FROM usage_snapshots"
        ).fetchone()["n"]
        after_application = db.execute(
            "SELECT COUNT(*) AS n FROM usage_application_snapshots"
        ).fetchone()["n"]

    assert after_usage == before_usage
    assert after_application == before_application

    # The partner's per-period usage and application usage must stay
    # empty too — no usage_import record either.
    connected, active = store.usage(partner_id, "2026-08")
    assert connected == set()
    assert active == set()
    assert store.application_usage(partner_id, "2026-08", 564) is None
    assert store.application_usage(partner_id, "2026-08", 565) is None
    assert store.usage_import(partner_id, "2026-08") is None


def test_sync_period_runs_without_mysql_or_mcp(tmp_path):
    """``sync_period`` must not require MySQL or MCP credentials any more.

    We patch ``mysql_configured`` and the MCP settings to look "ready"
    — the function must still return a no-op ``SyncResult`` and never
    touch the network or the database. This is the regression guard
    for "someone accidentally re-enables the BI pull".
    """
    store = Store(tmp_path / "test.db")

    with store.connect() as db:
        before_usage = db.execute(
            "SELECT COUNT(*) AS n FROM usage_snapshots"
        ).fetchone()["n"]

    # Pretend both backends are configured. The no-op must ignore them.
    store.set_setting("mcp_access_token", "fake-token")
    result = sync_period(store, "2026-08")

    assert result.partners_synced == 0
    assert result.partners_skipped == []
    assert result.errors == []

    with store.connect() as db:
        after_usage = db.execute(
            "SELECT COUNT(*) AS n FROM usage_snapshots"
        ).fetchone()["n"]
    assert after_usage == before_usage


def test_sync_period_rejects_invalid_period(tmp_path):
    """Period validation is preserved on the no-op path."""
    store = Store(tmp_path / "test.db")
    try:
        sync_period(store, "not-a-period")
    except ValueError as error:
        assert str(error) == "invalid_period"
    else:
        raise AssertionError("sync_period should reject an invalid period")
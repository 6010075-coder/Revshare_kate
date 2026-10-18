from __future__ import annotations

from revshare.sheet_partners import (
    iter_partners_from_rows,
    parse_rate,
    partner_rate_from_row,
)
from revshare.store import Store


SAMPLE_ROWS = [
    ["Name", "Type", "Legal entity"],
    ["Total"],
    [],
    ["Chatbots"],
    ["Message.help", "Сhatbot", "OU Wahelp (Эстония)"] + [""] * 31 + [0.15],
    ["Other"],
    ["Flocktory", "Реклама", "ТОО Wunder"] + [""] * 31 + [20],
    ["Smartmetrika", "Analytics", "ОП Кравченко"] + [""] * 31 + ["5 / 15 %"],
    ["Europe (Hungary) - Global"],
    ["Brazil", "", "", "", "", "1 руб = $", 77.22],
]


def test_parse_rate_handles_decimal_percent_and_text():
    assert parse_rate(0.15) == 0.15
    assert parse_rate("0.2") == 0.2
    assert parse_rate(20) == 0.2
    assert parse_rate("5 / 15 %") == 0.15
    assert parse_rate("0.001") == 0.001
    assert parse_rate("") is None


def test_iter_partners_from_rows_skips_sections():
    partners = iter_partners_from_rows(SAMPLE_ROWS)
    names = [partner.name for partner in partners]
    assert names == ["Message.help", "Flocktory", "Smartmetrika"]
    assert partners[0].rate == 0.15
    assert partners[1].rate == 0.2
    assert partners[2].rate == 0.15


def test_partner_rate_falls_back_to_earlier_month():
    row = ["Ziflow", "AiBot", "Too Ziflow"] + [""] * 31 + [0.2]
    assert partner_rate_from_row(row) == 0.2


def test_import_partners_from_sheet_creates_and_updates(tmp_path):
    from revshare.sheet_partners import import_partners_from_sheet

    store = Store(tmp_path / "revshare.db")
    store.add_partner(
        name="Message.help",
        legal_name="old",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="email",
        token="token",
        rate=0.1,
        currency="USD",
    )
    result = import_partners_from_sheet(store, rows=SAMPLE_ROWS, dry_run=False)
    assert "Flocktory" in result.created
    assert "Smartmetrika" in result.created
    assert "Message.help" in result.updated
    partner = next(row for row in store.partners() if row["name"] == "Message.help")
    assert partner["legal_name"] == "OU Wahelp (Эстония)"
    assert partner["rate"] == 0.15

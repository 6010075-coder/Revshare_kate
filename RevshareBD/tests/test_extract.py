from pathlib import Path

from openpyxl import Workbook

from revshare.extract import (
    SHEET_SKIP_TOKENS,
    parse_partner_report,
    period_sheet_titles,
)
from revshare.report_template import (
    FIRST_DATA_ROW,
    TRANSLATIONS,
    build_report_template_xlsx,
)
from revshare.store import Store


def test_parse_partner_report_groups_rows_by_application(tmp_path: Path):
    path = tmp_path / "report.csv"
    path.write_text(
        "Application,Location ID,Turnover,Currency\n"
        "Message.help,1001,100,USD\n"
        "AI Beauty Bot,1002,200,USD\n"
        "Message.help,1003,50,USD\n",
        encoding="utf-8",
    )

    parsed = parse_partner_report(path, rate=0.15, default_currency="USD", period="2026-08")

    assert parsed.turnover == 350
    assert parsed.currency == "USD"
    assert parsed.reported_share == 52.5
    assert parsed.location_ids == ["1001", "1002", "1003"]
    assert parsed.location_source == "application_table"
    # The 4-column CSV shape predates line_items — the parser still
    # surfaces application_breakdown aggregates so existing dashboards
    # keep working. line_items is added by the 9-column parser path.
    assert parsed.application_breakdown == [
        {
            "application_ref": "Message.help",
            "turnover": 150.0,
            "reported_share": 22.5,
            "currency": "USD",
            "location_ids": ["1001", "1003"],
        },
        {
            "application_ref": "AI Beauty Bot",
            "turnover": 200.0,
            "reported_share": 30.0,
            "currency": "USD",
            "location_ids": ["1002"],
        },
    ]


def test_parser_accepts_blank_template_without_leaking_examples(tmp_path: Path):
    """The blank primary sheet must not surface the example sheet rows.

    The 9-column template ships an "Пример отчёта" companion sheet for
    the partner to study. The parser must skip it so its example rows
    never leak into reconciliation.
    """
    store = Store(tmp_path / "t.db")
    pid = store.add_partner(
        name="Template Integration Hub App",
        legal_name="",
        legal_country="",
        altegio_legal_entity="",
        contact_name="",
        email="",
        telegram="",
        report_channel="telegram",
        token="tok",
        rate=0.2,
        currency="USD",
    )
    partner = store.partner(pid)

    blob = build_report_template_xlsx(store, partner, "2026-09", language="ru")
    path = tmp_path / "template.xlsx"
    path.write_bytes(blob)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    # The blank template has NO application/location rows in the data
    # area. The "Пример отчёта" sheet must be skipped, so neither turnover
    # nor location_ids can leak from it.
    assert parsed.application_breakdown == []
    assert "Пример" not in " ".join(parsed.location_ids)  # example rows absent


def test_parser_extracts_ten_column_template_with_line_items(tmp_path: Path):
    """A partner-filled 10-column workbook should yield per-row line items
    plus application breakdown. The parser must convert payment-currency
    amounts to the report currency using the row's fx rate and surface
    the settlement currency column.
    """
    path = tmp_path / "report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Report — September 2026"
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    # Row 1: USD payment, USD report currency — no FX conversion needed.
    ws.cell(row=11, column=1, value=312045)
    ws.cell(row=11, column=2, value="Beauty Loft, LLC")
    ws.cell(row=11, column=3, value="Month")
    ws.cell(row=11, column=4, value="USD")
    ws.cell(row=11, column=5, value=39.20)
    ws.cell(row=11, column=6, value=1.0)
    ws.cell(row=11, column=7, value="USD")  # settlement currency
    ws.cell(row=11, column=9, value=0.2)  # Altegio rate (column I)
    # Row 2: EUR payment, USD report currency — FX converts 45.30 → 49.01.
    ws.cell(row=12, column=1, value=199801)
    ws.cell(row=12, column=2, value="Nail Studio Lumière")
    ws.cell(row=12, column=3, value="Month")
    ws.cell(row=12, column=4, value="EUR")
    ws.cell(row=12, column=5, value=45.30)
    ws.cell(row=12, column=6, value=1.082)
    ws.cell(row=12, column=7, value="USD")  # partner kept settlement = USD
    ws.cell(row=12, column=9, value=0.2)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.currency == "USD"
    assert parsed.location_ids == ["312045", "199801"]
    expected_turnover = round(39.20 * 1.0 + 45.30 * 1.082, 2)
    assert parsed.turnover == expected_turnover
    assert parsed.reported_share == round(expected_turnover * 0.2, 2)

    line_items = []
    for group in parsed.application_breakdown:
        line_items.extend(group.get("line_items") or [])
    assert len(line_items) == 2
    first, second = line_items
    assert first["location_id"] == "312045"
    assert first["client_name"] == "Beauty Loft, LLC"
    assert first["subscription_period"] == "Month"
    assert first["payment_currency"] == "USD"
    assert first["amount_in_payment_currency"] == 39.2
    assert first["fx_rate"] == 1.0
    assert first["settlement_currency"] == "USD"
    assert first["amount_in_report_currency"] == 39.2
    assert first["altegio_rate"] == 0.2
    assert first["due_to_altegio"] == 7.84
    assert second["location_id"] == "199801"
    assert second["payment_currency"] == "EUR"
    assert second["amount_in_payment_currency"] == 45.3
    assert second["fx_rate"] == 1.082
    assert second["settlement_currency"] == "USD"
    assert second["amount_in_report_currency"] == round(45.30 * 1.082, 2)


def test_parser_skips_sample_sheet_in_workbook(tmp_path: Path):
    """A workbook with a "Sample report" sheet must NOT read its rows."""
    path = tmp_path / "report.xlsx"
    wb = Workbook()
    sample = wb.active
    sample.title = "Sample report (EN)"
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        sample.cell(row=10, column=column_index, value=header)
    sample.cell(row=11, column=1, value=312045)
    sample.cell(row=11, column=2, value="Beauty Loft")
    sample.cell(row=11, column=3, value="Month")
    sample.cell(row=11, column=4, value="USD")
    sample.cell(row=11, column=5, value=39.20)
    sample.cell(row=11, column=6, value=1.0)
    sample.cell(row=11, column=7, value="USD")
    sample.cell(row=11, column=9, value=0.2)

    real = wb.create_sheet("Report — September 2026")
    for column_index, header in enumerate(headers, start=1):
        real.cell(row=10, column=column_index, value=header)
    real.cell(row=11, column=1, value=501224)
    real.cell(row=11, column=2, value="Velvet SPA")
    real.cell(row=11, column=3, value="Month")
    real.cell(row=11, column=4, value="USD")
    real.cell(row=11, column=5, value=45.73)
    real.cell(row=11, column=6, value=1.0)
    real.cell(row=11, column=7, value="USD")
    real.cell(row=11, column=9, value=0.2)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.location_ids == ["501224"]
    line_items = [
        item
        for group in parsed.application_breakdown
        for item in group.get("line_items") or []
    ]
    assert len(line_items) == 1
    assert line_items[0]["client_name"] == "Velvet SPA"
    assert line_items[0]["settlement_currency"] == "USD"


def test_sheet_skip_tokens_cover_example_variants():
    """Defensive: parser must skip sheets whose title contains any of
    the documented sample/example tokens in any language.
    """
    assert "пример" in SHEET_SKIP_TOKENS
    assert "sample" in SHEET_SKIP_TOKENS
    assert "example" in SHEET_SKIP_TOKENS


def test_period_sheet_titles_includes_month_in_user_language():
    titles = period_sheet_titles("2026-09")
    assert "september 2026" in titles
    assert "сентябрь 2026" in titles
    assert "2026-09" in titles
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


# ---------------------------------------------------------------------------
# helpers for the FR-01..08 first-sheet parser tests
# ---------------------------------------------------------------------------


def _build_10_column_workbook(
    path: Path,
    *,
    language: str = "en",
    sheet_title: str | None = None,
    period_text: str | None = None,
    currency_text: str | None = None,
    rows: list[tuple] | None = None,
    add_total: bool = True,
    total_label_override: str | None = None,
    partner_name: str = "Acme Bookings, Inc.",
) -> Path:
    """Build a 10-column partner workbook mirroring the official template.

    ``rows`` is a list of ``(location_id, client, subscription, currency,
    amount, fx, rate)`` tuples — column I (rate) is optional and defaults
    to 0.2 when omitted. The builder emits B2/B3/B5 from the keyword
    arguments so tests can assert period / currency parsing directly.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title or "Report"
    headers = TRANSLATIONS[language]["headers"]
    ws.cell(row=2, column=2, value=partner_name)
    if period_text is not None:
        ws.cell(row=3, column=2, value=period_text)
    ws.cell(row=4, column=2, value=0.2)
    if currency_text is not None:
        ws.cell(row=5, column=2, value=currency_text)
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    for offset, data in enumerate(rows or []):
        location_id, client, subscription, currency, amount, fx = data[:6]
        rate = data[6] if len(data) >= 7 else 0.2
        row_number = FIRST_DATA_ROW + offset
        ws.cell(row=row_number, column=1, value=location_id)
        ws.cell(row=row_number, column=2, value=client)
        ws.cell(row=row_number, column=3, value=subscription)
        ws.cell(row=row_number, column=4, value=currency)
        ws.cell(row=row_number, column=5, value=amount)
        ws.cell(row=row_number, column=6, value=fx)
        ws.cell(row=row_number, column=7, value=currency_text or "USD")
        ws.cell(row=row_number, column=9, value=rate)
    if add_total:
        total_label = total_label_override or TRANSLATIONS[language]["total_label"]
        ws.cell(row=FIRST_DATA_ROW + len(rows or []), column=1, value=total_label)
    wb.save(path)
    return path


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


def test_parser_reads_only_first_sheet_and_ignores_companion_sample(tmp_path: Path):
    """FR-01: only ``workbook.worksheets[0]`` is read. The "Sample report"
    companion sheet is present in the workbook but the parser must NOT
    read its rows — even if it is the active sheet. The real report is
    placed first to mirror what the Integration Hub template ships.
    """
    path = tmp_path / "report.xlsx"
    wb = Workbook()
    real = wb.active
    real.title = "Report — September 2026"
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        real.cell(row=10, column=column_index, value=header)
    real.cell(row=2, column=2, value="Acme Bookings, Inc.")
    real.cell(row=3, column=2, value="September 2026")
    real.cell(row=5, column=2, value="USD")
    real.cell(row=11, column=1, value=501224)
    real.cell(row=11, column=2, value="Velvet SPA")
    real.cell(row=11, column=3, value="Month")
    real.cell(row=11, column=4, value="USD")
    real.cell(row=11, column=5, value=45.73)
    real.cell(row=11, column=6, value=1.0)
    real.cell(row=11, column=7, value="USD")
    real.cell(row=11, column=9, value=0.2)

    sample = wb.create_sheet("Sample report (EN)")
    sample.cell(row=10, column=1, value="Altegio location ID")
    sample.cell(row=10, column=2, value="Client name")
    sample.cell(row=10, column=3, value="Period")
    sample.cell(row=10, column=4, value="Payment currency")
    sample.cell(row=10, column=5, value="Amount")
    sample.cell(row=11, column=1, value=312045)
    sample.cell(row=11, column=2, value="Beauty Loft")
    sample.cell(row=11, column=5, value=99999.99)
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
    assert "Пример" not in " ".join(parsed.location_ids)


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


# ---------------------------------------------------------------------------
# FR-01..08 contract tests for the first-sheet parser
# ---------------------------------------------------------------------------


def test_parser_reads_only_first_sheet_when_second_is_sample(tmp_path: Path):
    """FR-01: a workbook whose first sheet is the real report and whose
    second sheet is "Sample report" must read only the first sheet's
    rows — even though the sample sheet is structurally identical.
    """
    path = tmp_path / "first_sheet_wins.xlsx"
    wb = Workbook()
    real = wb.active
    real.title = "Real report"
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        real.cell(row=10, column=column_index, value=header)
    real.cell(row=2, column=2, value="Acme Bookings, Inc.")
    real.cell(row=3, column=2, value="September 2026")
    real.cell(row=5, column=2, value="USD")
    real.cell(row=11, column=1, value=501224)
    real.cell(row=11, column=2, value="Velvet SPA")
    real.cell(row=11, column=3, value="Month")
    real.cell(row=11, column=4, value="USD")
    real.cell(row=11, column=5, value=45.73)
    real.cell(row=11, column=6, value=1.0)
    real.cell(row=11, column=7, value="USD")
    real.cell(row=11, column=9, value=0.2)

    sample = wb.create_sheet("Sample report (RU)")
    for column_index, header in enumerate(TRANSLATIONS["ru"]["headers"], start=1):
        sample.cell(row=10, column=column_index, value=header)
    sample.cell(row=11, column=1, value=312045)
    sample.cell(row=11, column=2, value="Beauty Loft")
    sample.cell(row=11, column=5, value=99999.99)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.location_ids == ["501224"]
    assert parsed.turnover == 45.73


def test_parser_reads_first_sheet_even_when_hidden_or_renamed(tmp_path: Path):
    """FR-01: sheet visibility, title, and active flag must not influence
    which sheet is read — only its position in ``workbook.worksheets``.
    """
    path = tmp_path / "hidden_first_sheet.xlsx"
    wb = Workbook()
    first = wb.active
    first.title = "Internal dump"
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        first.cell(row=10, column=column_index, value=header)
    first.cell(row=3, column=2, value="September 2026")
    first.cell(row=5, column=2, value="USD")
    first.cell(row=11, column=1, value=701122)
    first.cell(row=11, column=2, value="Hidden LLC")
    first.cell(row=11, column=5, value=12.50)
    first.cell(row=11, column=6, value=1.0)
    first.cell(row=11, column=9, value=0.2)
    first.sheet_state = "hidden"

    second = wb.create_sheet("Report — September 2026")
    second.cell(row=10, column=1, value="Some other header")
    second.cell(row=11, column=1, value=999999)
    second.cell(row=11, column=5, value=99999.99)
    # The second sheet must be the active one to prove the parser does
    # not pick the "active" sheet.
    wb.active = 1
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.location_ids == ["701122"]
    assert parsed.turnover == 12.50


def test_parser_returns_table_structure_error_when_first_sheet_unrecognised(
    tmp_path: Path,
):
    """FR-03: a first sheet that does not contain the 10-column header
    layout must yield ``error='table_structure_not_found'`` so the
    partner is asked to fix the file rather than silently falling back.
    """
    path = tmp_path / "garbage.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "This is not a partner report"
    ws["A2"] = "Random notes"
    ws["B3"] = "Not a period"
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error == "table_structure_not_found"
    assert parsed.turnover is None
    assert parsed.location_ids == []


def test_parser_parses_russian_period_from_b3(tmp_path: Path):
    """FR-02: B3 = «Сентябрь 2026» must yield ``period='2026-09'``."""
    path = tmp_path / "ru_period.xlsx"
    _build_10_column_workbook(
        path,
        language="ru",
        period_text="Сентябрь 2026",
        currency_text="USD",
        rows=[(501224, "Velvet SPA", "Месяц", "USD", 100.0, 1.0)],
    )

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.period == "2026-09"
    assert "period:2026-09" in parsed.notes
    assert parsed.turnover == 100.0


def test_parser_parses_english_period_from_b3(tmp_path: Path):
    """FR-02: B3 = «September 2026» must yield ``period='2026-09'``."""
    path = tmp_path / "en_period.xlsx"
    _build_10_column_workbook(
        path,
        language="en",
        period_text="September 2026",
        currency_text="USD",
        rows=[(501224, "Velvet SPA", "Month", "USD", 100.0, 1.0)],
    )

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.period == "2026-09"
    assert "period:2026-09" in parsed.notes
    assert parsed.turnover == 100.0


def test_parser_parses_numeric_period_formats_from_b3(tmp_path: Path):
    """FR-02: numeric variants like ``09.2026`` and ``2026/09`` must be
    recognised too — partner files are not always in a localised form.
    """
    for variant, expected_period in (
        ("09.2026", "2026-09"),
        ("2026/09", "2026-09"),
        ("2026-09", "2026-09"),
    ):
        path = tmp_path / f"numeric_{variant.replace('/', '_').replace('.', '_')}.xlsx"
        _build_10_column_workbook(
            path,
            language="en",
            period_text=variant,
            currency_text="USD",
            rows=[(501224, "Velvet SPA", "Month", "USD", 50.0, 1.0)],
        )
        parsed = parse_partner_report(
            path, rate=0.2, default_currency="USD", period=expected_period
        )
        assert parsed.period == expected_period, variant


def test_parser_uses_b3_over_period_argument(tmp_path: Path):
    """FR-02: ``period`` argument is a hint only — B3 is the source of
    truth. When they disagree, B3 wins.
    """
    path = tmp_path / "b3_wins.xlsx"
    _build_10_column_workbook(
        path,
        language="en",
        period_text="September 2026",
        currency_text="USD",
        rows=[(501224, "Velvet SPA", "Month", "USD", 10.0, 1.0)],
    )

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2025-01"
    )

    assert parsed.period == "2026-09"


def test_parser_returns_period_not_found_when_b3_blank(tmp_path: Path):
    """FR-02: missing B3 must not invent a period — ``period=None`` and
    a ``period_not_found`` note. ``error`` stays ``None`` because this
    is recoverable at the partner level (re-upload with a corrected
    period) — but no fallback to the current month is performed.
    """
    path = tmp_path / "missing_b3.xlsx"
    _build_10_column_workbook(
        path,
        language="en",
        period_text=None,
        currency_text="USD",
        rows=[(501224, "Velvet SPA", "Month", "USD", 100.0, 1.0)],
    )

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.period is None
    assert parsed.error is None  # recoverable, not fatal
    assert "period_not_found" in parsed.notes
    assert parsed.turnover == 100.0  # row is still counted


def test_parser_skips_blank_rows_between_clients(tmp_path: Path):
    """FR-05: blank rows inside the table must not stop processing —
    clients after the gap are still counted.
    """
    path = tmp_path / "gappy_rows.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    # Client at row 11, blank at row 12, two more clients at 13-14.
    for row_number, (location_id, client, amount) in enumerate(
        [
            (501001, "First", 10.0),
            (501002, "Second", 20.0),
            (501003, "Third", 30.0),
        ],
        start=11,
    ):
        ws.cell(row=row_number, column=1, value=location_id)
        ws.cell(row=row_number, column=2, value=client)
        ws.cell(row=row_number, column=4, value="USD")
        ws.cell(row=row_number, column=5, value=amount)
        ws.cell(row=row_number, column=6, value=1.0)
        ws.cell(row=row_number, column=7, value="USD")
        ws.cell(row=row_number, column=9, value=0.2)
        if row_number == 11:
            ws.cell(row=12, column=1, value=None)  # explicit blank gap
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.location_ids == ["501001", "501002", "501003"]
    assert parsed.turnover == 60.0


def test_parser_reads_extra_rows_past_static_template_total(tmp_path: Path):
    """FR-05: when the partner adds rows beyond the template's
    ``TOTAL_ROW`` (61) the parser must keep counting until it sees the
    actual «ИТОГО» / «TOTAL» row. The cached Excel formula referencing
    A11:E60 must NOT be trusted as the source of truth.
    """
    path = tmp_path / "eighty_clients.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Report — September 2026"
    headers = TRANSLATIONS["en"]["headers"]
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    for offset in range(80):
        row_number = 11 + offset
        ws.cell(row=row_number, column=1, value=600000 + offset)
        ws.cell(row=row_number, column=2, value=f"Client {offset}")
        ws.cell(row=row_number, column=4, value="USD")
        ws.cell(row=row_number, column=5, value=10.0 + offset)
        ws.cell(row=row_number, column=6, value=1.0)
        ws.cell(row=row_number, column=7, value="USD")
        ws.cell(row=row_number, column=9, value=0.2)
    # Total row at 91 — well past the static ``TOTAL_ROW=61``. The
    # partner re-anchored the SUM formula to reference A11:E90 only,
    # but the parser must include all 80 rows.
    ws.cell(row=91, column=1, value="TOTAL in report currency")
    ws.cell(row=91, column=8, value=f"=SUM(H11:H90)")
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert len(parsed.location_ids) == 80
    assert parsed.location_ids[0] == "600000"
    assert parsed.location_ids[-1] == "600079"
    # 10 + 11 + … + 89 = (10 + 89) * 80 / 2 = 3960.00
    assert parsed.turnover == 3960.0
    assert parsed.reported_share == round(3960.0 * 0.2, 2)
    # The TOTAL row itself must not be counted as a client.
    assert "TOTAL" not in parsed.location_ids


def test_parser_treats_template_only_rows_as_empty_report(tmp_path: Path):
    """FR-06: the empty primary sheet (formulas + formatting, no data)
    is a valid empty report — not an error. ``turnover=0`` and
    ``location_ids=[]`` confirm the result.
    """
    path = tmp_path / "empty_template_only.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    # Mirror the official template: settlement currency + rate are
    # prefilled, formulas in H/J for empty rows. No real client data.
    for offset in range(50):
        row_number = 11 + offset
        ws.cell(row=row_number, column=7, value="USD")
        ws.cell(row=row_number, column=9, value=0.2)
        ws.cell(row=row_number, column=8, value=f"=E{row_number}*F{row_number}")
        ws.cell(row=row_number, column=10, value=f"=H{row_number}*I{row_number}")
    ws.cell(row=61, column=1, value="TOTAL in report currency")
    ws.cell(row=61, column=8, value=f"=SUM(H11:H60)")
    ws.cell(row=61, column=10, value=f"=SUM(J11:J60)")
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error is None
    assert parsed.location_source == "empty_report"
    assert parsed.turnover == 0.0
    assert parsed.reported_share == 0.0
    assert parsed.location_ids == []
    assert parsed.application_breakdown == []
    assert "empty_report" in parsed.notes


def test_parser_accepts_row_with_zero_amount_and_missing_fx(tmp_path: Path):
    """FR-07: a client with E=0 and F empty is valid — ``converted=0``
    regardless of missing FX. The row is not flagged as an error.
    """
    path = tmp_path / "zero_amount.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    ws.cell(row=11, column=1, value=501224)
    ws.cell(row=11, column=2, value="Trial salon")
    ws.cell(row=11, column=3, value="Trial (no payment)")
    ws.cell(row=11, column=4, value="USD")
    ws.cell(row=11, column=5, value=0)
    # F (column 6) is intentionally blank.
    ws.cell(row=11, column=7, value="USD")
    ws.cell(row=11, column=9, value=0.2)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error is None
    assert parsed.location_ids == ["501224"]
    line_items = [
        item
        for group in parsed.application_breakdown
        for item in group.get("line_items") or []
    ]
    assert len(line_items) == 1
    assert line_items[0]["amount_in_payment_currency"] == 0
    assert line_items[0]["amount_in_report_currency"] == 0
    assert line_items[0]["due_to_altegio"] == 0


def test_parser_flags_missing_amount_as_error_row(tmp_path: Path):
    """FR-07: a client row whose column E is empty must be listed in
    ``error_rows`` and the report cannot be confirmed (``error`` set).
    """
    path = tmp_path / "missing_amount.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    ws.cell(row=11, column=1, value=501224)
    ws.cell(row=11, column=2, value="Velvet SPA")
    ws.cell(row=11, column=4, value="USD")
    # E (column 5) intentionally blank.
    ws.cell(row=12, column=1, value=600012)
    ws.cell(row=12, column=2, value="Velvet SPA 2")
    ws.cell(row=12, column=4, value="USD")
    ws.cell(row=12, column=5, value=12.50)
    ws.cell(row=12, column=6, value=1.0)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error == "rows_with_missing_amount"
    assert parsed.turnover is None
    assert parsed.error_rows == [11]


def test_parser_flags_garbage_amount_as_error_row(tmp_path: Path):
    """FR-07: a client row whose column E contains unparseable text
    (``"abc"``) must be listed in ``error_rows``. The report is not
    confirmed and the totals are not trusted.
    """
    path = tmp_path / "garbage_amount.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    ws.cell(row=12, column=1, value=600012)
    ws.cell(row=12, column=2, value="Bad row")
    ws.cell(row=12, column=4, value="USD")
    ws.cell(row=12, column=5, value="abc")
    ws.cell(row=12, column=6, value=1.0)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error == "rows_with_missing_amount"
    assert parsed.turnover is None
    assert 12 in parsed.error_rows


def test_parser_collects_all_error_rows_not_just_the_first(tmp_path: Path):
    """FR-07: multiple bad rows are accumulated so the partner can
    fix them in one pass.
    """
    path = tmp_path / "multiple_errors.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    for row_number, amount in (
        (11, 10.0),  # valid
        (12, None),  # empty
        (13, "nope"),  # garbage
        (14, 40.0),  # valid
    ):
        ws.cell(row=row_number, column=1, value=600000 + row_number)
        ws.cell(row=row_number, column=2, value=f"Client {row_number}")
        ws.cell(row=row_number, column=4, value="USD")
        if amount is not None:
            ws.cell(row=row_number, column=5, value=amount)
        ws.cell(row=row_number, column=6, value=1.0)
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error == "rows_with_missing_amount"
    assert parsed.turnover is None
    assert parsed.error_rows == [12, 13]


def test_parser_returns_distinct_empty_report_not_error_for_blank_header(
    tmp_path: Path,
):
    """FR-06: a structurally valid but empty report must NOT be
    reported as an error — turnover=0 with location_ids=[] is the
    expected result.
    """
    path = tmp_path / "just_header.xlsx"
    wb = Workbook()
    ws = wb.active
    headers = TRANSLATIONS["en"]["headers"]
    ws.cell(row=3, column=2, value="September 2026")
    ws.cell(row=5, column=2, value="USD")
    for column_index, header in enumerate(headers, start=1):
        ws.cell(row=10, column=column_index, value=header)
    # No client rows at all.
    wb.save(path)

    parsed = parse_partner_report(
        path, rate=0.2, default_currency="USD", period="2026-09"
    )

    assert parsed.error is None
    assert parsed.location_source == "empty_report"
    assert parsed.turnover == 0.0
    assert parsed.reported_share == 0.0
    assert parsed.location_ids == []


def test_parser_uses_b5_currency_first_then_default(tmp_path: Path):
    """FR-08 + the B5 contract: the report currency is read from B5 of
    the first sheet. When B5 is missing the parser falls back to the
    partner's contractual currency.
    """
    # Case 1: B5 set to EUR.
    path_eur = tmp_path / "b5_eur.xlsx"
    _build_10_column_workbook(
        path_eur,
        language="en",
        period_text="September 2026",
        currency_text="EUR",
        rows=[(501224, "Velvet SPA", "Month", "EUR", 50.0, 1.0)],
    )
    parsed_eur = parse_partner_report(
        path_eur, rate=0.2, default_currency="USD", period="2026-09"
    )
    assert parsed_eur.currency == "EUR"
    # Case 2: B5 missing — fallback to default_currency=KZT.
    path_default = tmp_path / "no_b5.xlsx"
    _build_10_column_workbook(
        path_default,
        language="en",
        period_text="September 2026",
        currency_text=None,
        rows=[(501224, "Velvet SPA", "Month", "USD", 50.0, 1.0)],
    )
    parsed_default = parse_partner_report(
        path_default, rate=0.2, default_currency="KZT", period="2026-09"
    )
    assert parsed_default.currency == "KZT"
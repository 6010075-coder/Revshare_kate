"""Build the partner-facing revshare report template (.xlsx).

The helper attaches the template to the first Telegram report request
(``notice_key='invite'``).

Centralising the XLSX construction keeps the partner-facing schema consistent.

Template layout (two sheets):

1. **Primary sheet** — empty form on the partner's language. The sheet
   title embeds the reporting month ("Отчёт за Сентябрь 2026" /
   "Report for September 2026") so the parser's
   ``period_sheet_titles`` filter recognises it as the active month and
   skips any "example" / "sample" companion sheet.
2. **Example sheet** — a filled-in reference ("Пример отчёта") the
   partner can study. The parser explicitly ignores sheets whose
   normalised title contains "пример" / "example" / "sample".

Both sheets share the same ten-column schema:

| Col | Header                                     |
|-----|--------------------------------------------|
|  A  | Altegio location ID                        |
|  B  | Client name or client email                |
|  C  | Paid subscription period                   |
|  D  | Payment currency                           |
|  E  | Received by partner (in payment currency)  |
|  F  | Monthly avg rate to report currency        |
|  G  | Settlement currency with Altegio           |
|  H  | Received by partner (report currency)      |
|  I  | Altegio rate                               |
|  J  | Due to Altegio (report currency)           |

"Report currency" is read from ``partner.currency`` — the partner's
contractual currency. ``partner.rate`` (Altegio's revenue-share slice)
is prefilled in column I; the partner may overwrite it per row.
Column G ("Settlement currency with Altegio") is prefilled with
``partner.currency`` on the primary sheet (USD on the example sheet),
so reviewers can see per-row which currency Altegio will be paid in.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from revshare.i18n import translator
from revshare.store import Store


# Visual palette — mirrors the Kate-supplied reference template.
TITLE_FONT = Font(name="Calibri", size=18, bold=True, color="FF1F1F1F")
LABEL_FONT = Font(name="Calibri", size=10, bold=False, color="FF666666")
VALUE_BOLD_FONT = Font(name="Calibri", size=12, bold=True, color="FF1F1F1F")
VALUE_PLAIN_FONT = Font(name="Calibri", size=11, bold=False, color="FF1F1F1F")
DATA_FONT = Font(name="Calibri", size=11, bold=False, color="FF1F1F1F")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")
DISCLAIMER_HEADER_FONT = Font(name="Calibri", size=12, bold=True, color="FF1F1F1F")
DISCLAIMER_BODY_FONT = Font(name="Calibri", size=11, bold=False, color="FF666666")

YELLOW_HIGHLIGHT = PatternFill("solid", fgColor="FFFF00")
HEADER_FILL = PatternFill("solid", fgColor="FF3A3A3A")
DATA_FILL = PatternFill("solid", fgColor="FFF7F7F7")
DISCLAIMER_BODY_FILL = PatternFill("solid", fgColor="FFEAF3FF")
THIN = Side(style="thin", color="FFD9DDE8")
DATA_BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
LINE = Border(bottom=THIN)


# Column widths tuned for the ten-column schema (with the new
# "Settlement currency with Altegio" column at G).
COLUMN_WIDTHS = {
    "A": 16,  # Altegio location ID
    "B": 32,  # Client name or client email
    "C": 18,  # Paid subscription period
    "D": 12,  # Payment currency
    "E": 18,  # Received by partner (in payment currency)
    "F": 14,  # Monthly avg rate to report currency
    "G": 16,  # Settlement currency with Altegio (partner.currency)
    "H": 18,  # Received by partner (report currency)
    "I": 12,  # Altegio rate
    "J": 18,  # Due to Altegio (report currency)
}

DATA_ROW_COUNT = 50
FIRST_DATA_ROW = 11
LAST_DATA_ROW = FIRST_DATA_ROW + DATA_ROW_COUNT - 1  # 60
TOTAL_ROW = LAST_DATA_ROW + 1  # 61

DISCLAIMER_HEADER_ROW = TOTAL_ROW + 3  # 64
DISCLAIMER_BODY_ROW = DISCLAIMER_HEADER_ROW + 1  # 65


TRANSLATIONS: dict[str, dict[str, object]] = {
    "ru": {
        "sheet_name": "Отчёт (RU)",
        "example_sheet_name": "Пример отчёта (RU)",
        "title": "Отчёт партнера Хаба Интеграций",
        "partner_label": "Партнер",
        "period_label": "Период",
        "rate_label": "Ставка Altegio (revenue share)",
        "report_currency_label": "Валюта отчёта",
        "contact_label": "Контактное лицо",
        "email_label": "Email для вопросов",
        "generated_label": "Файл сформирован",
        "headers": [
            "ID филиала\nAltegio",
            "Название клиента\nили Email клиента",
            "Период оплаченной\nподписки",
            "Валюта\nплатежа",
            "Получено партнёром\n(в валюте платежа)",
            "Среднемес. курс\nк валюте отчёта",
            "Валюта расчёта\nс Altegio",
            "Получено партнёром\n(валюта отчёта)",
            "Ставка\nAltegio",
            "К выплате Altegio\n(валюта отчёта)",
        ],
        "total_label": "ИТОГО в валюте отчёта",
        "disclaimer_header": "Ответственность партнёра за достоверность данных",
        "disclaimer_body": (
            "Отправляя этот отчёт, партнёр принимает на себя ответственность за "
            "достоверность указанных в нём данных. В частности, партнёр "
            "подтверждает следующее:\n"
            "  · В таблице выше перечислены ВСЕ клиенты Altegio, которые "
            "пользовались интеграцией партнёра в указанном периоде; других "
            "клиентов Altegio у партнёра нет.\n"
            "  · Суммы в колонке «Получено партнёром (в валюте платежа)» равны "
            "фактическим деньгам, полученным от каждого клиента за период, за "
            "вычетом возвратов.\n"
            "  · Конвертация в валюту отчёта произведена по среднемесячному "
            "курсу ЦБ страны валюты за отчётный месяц.\n"
            "  · Колонка «К выплате Altegio» рассчитана по действующей ставке "
            "договора.\n"
            "  · При обнаружении расхождений между этим отчётом и данными "
            "Altegio (установки в маркетплейсе, активность клиентов) партнёр "
            "предоставляет первичные документы в течение 5 рабочих дней.\n"
            "  · Намеренное искажение данных является основанием для "
            "приостановки приложения в маркетплейсе и пересмотра условий "
            "партнёрства."
        ),
        "example_rows": [
            (312045, "Beauty Loft, LLC", "Месяц", "USD", 39.20, 1.0, 0.2),
            (408733, "Wax & Go Studio", "Триал (без оплаты)", "USD", 0, 1.0, 0.2),
            (215890, "Atlas Barbershop", "Возврат", "EUR", -20, 1.082, 0.2),
            (501224, "Velvet SPA", "Месяц", "USD", 45.73, 1.0, 0.2),
            (199801, "Nail Studio Lumière", "Месяц", "EUR", 45.30, 1.082, 0.2),
            (322117, "Dental+ Clinic", "3 месяца", "USD", 147, 1.0, 0.2),
            (660104, "Salon Provence", "Месяц", "EUR", 36.20, 1.082, 0.2),
            (845512, "Beauty Hub", "Год", "EUR", 408, 1.082, 0.2),
            (102233, "Massage & Reset", "Месяц", "EUR", 45.30, 1.082, 0.2),
            (778901, "Aria Dance School", "Месяц", "USD", 49, 1.0, 0.2),
        ],
    },
    "en": {
        "sheet_name": "Report (EN)",
        "example_sheet_name": "Sample report (EN)",
        "title": "Integration Hub Partner Report",
        "partner_label": "Partner",
        "period_label": "Period",
        "rate_label": "Altegio rate (revenue share)",
        "report_currency_label": "Report currency",
        "contact_label": "Contact person",
        "email_label": "Email for questions",
        "generated_label": "File generated",
        "headers": [
            "Altegio\nlocation ID",
            "Client name\nor client email",
            "Paid subscription\nperiod",
            "Payment\ncurrency",
            "Received by partner\n(in payment currency)",
            "Monthly avg rate\nto report currency",
            "Settlement currency\nwith Altegio",
            "Received by partner\n(report currency)",
            "Altegio\nrate",
            "Due to Altegio\n(report currency)",
        ],
        "total_label": "TOTAL in report currency",
        "disclaimer_header": "Partner's responsibility for data accuracy",
        "disclaimer_body": (
            "By submitting this report, the partner assumes responsibility for "
            "the accuracy of the data above. In particular, the partner "
            "confirms that:\n"
            "  · The table lists EVERY Altegio customer who used the partner's "
            "integration during the reported period; no other Altegio "
            "customers exist on the partner's side.\n"
            "  · The amounts in the 'Received by partner (in payment currency)' "
            "column equal the actual money received from each customer for the "
            "period, net of refunds.\n"
            "  · Conversion to the report currency used the central bank's "
            "average monthly rate for the currency of the reported month.\n"
            "  · The 'Due to Altegio' column is calculated using the current "
            "contractual rate.\n"
            "  · If discrepancies are found between this report and Altegio "
            "data (marketplace installations, customer activity), the partner "
            "provides supporting documents within 5 business days.\n"
            "  · Intentional misrepresentation is grounds for suspension of "
            "the marketplace application and a review of partnership terms."
        ),
        "example_rows": [
            (312045, "Beauty Loft, LLC", "Month", "USD", 39.20, 1.0, 0.2),
            (408733, "Wax & Go Studio", "Trial (no payment)", "USD", 0, 1.0, 0.2),
            (215890, "Atlas Barbershop", "Refund", "EUR", -20, 1.082, 0.2),
            (501224, "Velvet SPA", "Month", "USD", 45.73, 1.0, 0.2),
            (199801, "Nail Studio Lumière", "Month", "EUR", 45.30, 1.082, 0.2),
            (322117, "Dental+ Clinic", "3 months", "USD", 147, 1.0, 0.2),
            (660104, "Salon Provence", "Month", "EUR", 36.20, 1.082, 0.2),
            (845512, "Beauty Hub", "Year", "EUR", 408, 1.082, 0.2),
            (102233, "Massage & Reset", "Month", "EUR", 45.30, 1.082, 0.2),
            (778901, "Aria Dance School", "Month", "USD", 49, 1.0, 0.2),
        ],
    },
}


# Re-exported header lists for tests and partner-facing docs. These
# mirror TRANSLATIONS[language]["headers"] exactly; we expose them at
# module level so external assertions don't have to dig into the dict.
EXPECTED_HEADERS_RU: tuple[str, ...] = tuple(
    TRANSLATIONS["ru"]["headers"]  # type: ignore[arg-type]
)
EXPECTED_HEADERS_EN: tuple[str, ...] = tuple(
    TRANSLATIONS["en"]["headers"]  # type: ignore[arg-type]
)


def _format_period(period: str, language: str) -> str:
    """Render ``2026-09`` as ``Сентябрь 2026`` / ``September 2026``."""
    if not period or len(period) < 7 or period[4] != "-":
        return period
    year, month = period[:4], period[5:7]
    t = translator(language)
    name = t(f"month_{int(month):02d}")
    return f"{name} {year}" if name else period


def _sheet_title_for_period(texts: dict[str, object], period: str, language: str) -> str:
    """Compose the primary sheet title with the reporting month in it.

    The month suffix is essential: ``extract.period_sheet_titles`` uses
    it to filter out example/sample companion sheets so the parser
    only sees the partner's actual report.
    """
    base = str(texts["sheet_name"]).split(" — ", 1)[0]
    return f"{base} — {_format_period(period, language)}"


def _render_sheet(
    worksheet,
    partner: dict,
    period: str,
    language: str,
    *,
    example: bool,
) -> None:
    texts = TRANSLATIONS[language]
    report_currency = str(partner.get("currency") or "USD").upper()
    partner_name = str(partner.get("name") or "").strip()
    contact_name = str(partner.get("contact_name") or "").strip()
    contact_email = str(partner.get("email") or "").strip()
    rate_value = partner.get("rate")
    rate_set = rate_value is not None and rate_value != ""

    if example:
        worksheet.title = str(texts["example_sheet_name"])
        display_name = partner_name or "Acme Bookings, Inc."
        display_contact = contact_name or "Daniel Carter, CFO"
        display_email = contact_email or "billing@acme.io"
    else:
        worksheet.title = _sheet_title_for_period(texts, period, language)
        display_name = partner_name
        display_contact = contact_name
        display_email = contact_email

    worksheet.sheet_view.showGridLines = False

    # --- Title (row 1) ---
    worksheet["A1"] = texts["title"]
    worksheet["A1"].font = TITLE_FONT
    worksheet["A1"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    worksheet.row_dimensions[1].height = 27.75

    # --- Header block (rows 2-7) ---
    header_rows = [
        ("A2", texts["partner_label"], "B2", display_name, True),
        ("A3", texts["period_label"], "B3", _format_period(period, language), True),
        ("A4", texts["rate_label"], "B4", float(rate_value) if rate_set else None, False),
        ("A5", texts["report_currency_label"], "B5", report_currency, True),
        ("A6", texts["contact_label"], "B6", display_contact or None, False),
        ("A7", texts["email_label"], "B7", display_email or None, False),
    ]
    for label_ref, label, value_ref, value, bold in header_rows:
        worksheet[label_ref] = label
        worksheet[label_ref].font = LABEL_FONT
        worksheet[label_ref].alignment = Alignment(horizontal="left", vertical="center")
        worksheet[value_ref] = value
        if bold:
            worksheet[value_ref].fill = YELLOW_HIGHLIGHT
        worksheet[value_ref].font = VALUE_BOLD_FONT if bold else VALUE_PLAIN_FONT
        worksheet[value_ref].alignment = Alignment(horizontal="left", vertical="center")
        worksheet.merge_cells(f"{value_ref}:J{value_ref[1:]}")
        if label_ref == "A4":
            worksheet[value_ref].number_format = "0.00"

    # Row 8 — File generated timestamp (UTC).
    worksheet["A8"] = texts["generated_label"]
    worksheet["A8"].font = LABEL_FONT
    worksheet["A8"].alignment = Alignment(horizontal="left", vertical="center")
    worksheet["B8"] = datetime.now(timezone.utc).strftime("%Y-%m-%d (UTC)")
    worksheet["B8"].font = VALUE_PLAIN_FONT
    worksheet["B8"].alignment = Alignment(horizontal="left", vertical="center")
    worksheet.merge_cells("B8:J8")

    # --- Table header (row 10) ---
    headers = texts["headers"]
    assert isinstance(headers, list) and len(headers) == 10
    for column_index, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=10, column=column_index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = DATA_BORDER
    worksheet.row_dimensions[10].height = 36

    # --- Data rows (11..60) ---
    rate_per_row = float(rate_value) if rate_set else None
    example_rows = texts.get("example_rows") if example else None
    # The example sheet hard-codes USD as the settlement currency so it
    # is independent of the partner's contract. The primary (empty) form
    # prefills partner.currency.
    settlement_currency = "USD" if example else report_currency

    for offset in range(DATA_ROW_COUNT):
        row_number = FIRST_DATA_ROW + offset
        is_example = example_rows is not None and offset < len(example_rows)
        for column_index in range(1, 11):
            cell = worksheet.cell(row=row_number, column=column_index)
            cell.font = DATA_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=column_index == 2)
            cell.border = LINE
        if is_example and isinstance(example_rows, list):
            row_data = example_rows[offset]
            assert isinstance(row_data, tuple) and len(row_data) == 7
            (loc_id, client, sub_period, currency, amount, fx, row_rate) = row_data
            worksheet.cell(row=row_number, column=1, value=loc_id)
            worksheet.cell(row=row_number, column=2, value=client)
            worksheet.cell(row=row_number, column=3, value=sub_period)
            worksheet.cell(row=row_number, column=4, value=currency)
            worksheet.cell(row=row_number, column=5, value=amount)
            worksheet.cell(row=row_number, column=6, value=fx)
            worksheet.cell(row=row_number, column=7, value=settlement_currency)
            worksheet.cell(row=row_number, column=8, value=f"=E{row_number}*F{row_number}")
            worksheet.cell(row=row_number, column=9, value=row_rate)
            worksheet.cell(row=row_number, column=10, value=f"=H{row_number}*I{row_number}")
        else:
            # Empty primary sheet: prefilled rate in column I (Altegio
            # rate) and settlement currency in column G. Everything else
            # blank for the partner to fill.
            if rate_per_row is not None:
                worksheet.cell(row=row_number, column=9, value=rate_per_row)
                worksheet.cell(row=row_number, column=9).number_format = "0.00"
            worksheet.cell(row=row_number, column=7, value=settlement_currency)
            worksheet.cell(row=row_number, column=6).number_format = "0.0000"
        worksheet.cell(row=row_number, column=5).number_format = "#,##0.00"
        worksheet.cell(row=row_number, column=8).number_format = "#,##0.00"
        worksheet.cell(row=row_number, column=10).number_format = "#,##0.00"

    # --- TOTAL row (61) ---
    total_label = texts["total_label"]
    worksheet.cell(row=TOTAL_ROW, column=1, value=total_label)
    worksheet.cell(row=TOTAL_ROW, column=1).font = Font(name="Calibri", size=11, bold=True, color="FF1F1F1F")
    worksheet.cell(row=TOTAL_ROW, column=1).alignment = Alignment(horizontal="left", vertical="center")
    worksheet.cell(row=TOTAL_ROW, column=1).fill = DATA_FILL
    for column_index in range(2, 11):
        worksheet.cell(row=TOTAL_ROW, column=column_index).fill = DATA_FILL
    worksheet.cell(row=TOTAL_ROW, column=8, value=f"=SUM(H{FIRST_DATA_ROW}:H{LAST_DATA_ROW})")
    worksheet.cell(row=TOTAL_ROW, column=10, value=f"=SUM(J{FIRST_DATA_ROW}:J{LAST_DATA_ROW})")
    worksheet.cell(row=TOTAL_ROW, column=8).font = Font(name="Calibri", size=11, bold=True, color="FF1F1F1F")
    worksheet.cell(row=TOTAL_ROW, column=10).font = Font(name="Calibri", size=11, bold=True, color="FF1F1F1F")
    worksheet.cell(row=TOTAL_ROW, column=8).number_format = "#,##0.00"
    worksheet.cell(row=TOTAL_ROW, column=10).number_format = "#,##0.00"

    # --- Disclaimer (rows 64-65) ---
    worksheet.cell(row=DISCLAIMER_HEADER_ROW, column=1, value=texts["disclaimer_header"])
    worksheet.cell(row=DISCLAIMER_HEADER_ROW, column=1).font = DISCLAIMER_HEADER_FONT
    worksheet.cell(row=DISCLAIMER_HEADER_ROW, column=1).alignment = Alignment(horizontal="left", vertical="center")
    worksheet.merge_cells(start_row=DISCLAIMER_HEADER_ROW, start_column=1, end_row=DISCLAIMER_HEADER_ROW, end_column=10)

    worksheet.cell(row=DISCLAIMER_BODY_ROW, column=1, value=texts["disclaimer_body"])
    worksheet.cell(row=DISCLAIMER_BODY_ROW, column=1).font = DISCLAIMER_BODY_FONT
    worksheet.cell(row=DISCLAIMER_BODY_ROW, column=1).alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    worksheet.cell(row=DISCLAIMER_BODY_ROW, column=1).fill = DISCLAIMER_BODY_FILL
    worksheet.merge_cells(start_row=DISCLAIMER_BODY_ROW, start_column=1, end_row=DISCLAIMER_BODY_ROW, end_column=10)
    worksheet.row_dimensions[DISCLAIMER_BODY_ROW].height = 90

    # --- Visual layout ---
    for letter, width in COLUMN_WIDTHS.items():
        worksheet.column_dimensions[letter].width = width
    worksheet.freeze_panes = "A11"


def build_report_template_xlsx(
    store: Store,
    partner: dict,
    period: str,
    language: str = "ru",
) -> bytes:
    """Render the partner-facing report template as bytes.

    Two sheets are emitted:
    1. The primary sheet on the partner's language with the reporting month
       in its title (so the parser recognises it).
    2. A sample/example sheet ("Пример отчёта") the partner can study.
       The parser explicitly skips sheets whose normalised title contains
       "пример" / "example" / "sample" so the example never leaks into
       reconciliation.
    """
    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)

    # Languages we translate end-to-end: ru + en. Other locales fall back
    # to ru (closer alphabet and UX feel for Cyrillic-script partners).
    primary = "ru" if language in {"ru", "uk", "pl", "hu", "pt"} else "en"

    primary_sheet = workbook.create_sheet(title="placeholder")
    _render_sheet(primary_sheet, partner, period, primary, example=False)

    example_sheet = workbook.create_sheet(title="placeholder")
    _render_sheet(example_sheet, partner, period, "ru", example=True)

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()

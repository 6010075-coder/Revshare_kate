from pathlib import Path

from openpyxl import Workbook

from revshare.extract import amounts_from_text, extract_file, extract_xlsx
from revshare.preview import build_draft
from revshare.registry import Partner, Registry


def test_amounts_prefer_labeled_total():
    text = "Services 12\nTotal GMV 1 250.50 USD"
    found = amounts_from_text(text)
    assert found[0].amount == 1250.50
    assert found[0].currency == "USD"
    assert found[0].confidence == "medium"


def test_plain_number_message(tmp_path: Path):
    path = tmp_path / "ficher.txt"
    path.write_text("Ficher август: 8400 EUR\n", encoding="utf-8")
    report = extract_file(path)
    assert report.candidates[0].amount == 8400
    assert report.candidates[0].currency == "EUR"


def test_xlsx_total_row(tmp_path: Path):
    path = tmp_path / "partner.xlsx"
    book = Workbook()
    sheet = book.active
    sheet["A1"] = "Item"
    sheet["B1"] = "Amount"
    sheet["A2"] = "Licenses"
    sheet["B2"] = 100
    sheet["A3"] = "Total"
    sheet["B3"] = 1500
    book.save(path)
    report = extract_xlsx(path)
    hinted = [c for c in report.candidates if c.confidence == "medium"]
    assert hinted
    assert hinted[0].amount == 1500


def test_draft_marks_unknown_when_registry_empty():
    registry = Registry(
        sheet={"id": "abc", "year_tabs": {2026: {"title": "2026"}}},
        write_policy={},
        deadline={},
        partners=[],
        path=Path("partners.yaml"),
    )
    from revshare.extract import ExtractedReport, AmountCandidate

    reports = [
        ExtractedReport(
            source="inbox/x.txt",
            partner_hint="unknown",
            candidates=[AmountCandidate(amount=10, raw="10", currency="USD")],
        )
    ]
    draft = build_draft("2026-08", reports, registry)
    assert draft["items"][0]["matched_partner_id"] is None
    assert draft["unmapped_partners"] is True
    assert draft["status"] == "preview_only"


def test_match_alias():
    partner = Partner(id="ficher", name="Ficher", aliases=["фичер"])
    registry = Registry({}, {}, {}, [partner], Path("partners.yaml"))
    assert registry.match("отчет Фичер август")[0].id == "ficher"

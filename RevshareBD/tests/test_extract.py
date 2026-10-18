from pathlib import Path

from revshare.extract import parse_partner_report


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

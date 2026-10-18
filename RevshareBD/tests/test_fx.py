from datetime import date

from revshare.fx import average_usd_rates, fetch_month_average_rates, to_usd, valid_period
from revshare.store import Store


def test_average_usd_rates_uses_the_mean_of_the_month():
    averaged = average_usd_rates(
        [
            {"EUR": 1.0, "UAH": 40.0},
            {"EUR": 0.8, "UAH": 42.0},
        ]
    )
    assert averaged["EUR"] == 0.9
    assert averaged["UAH"] == 41.0


def test_month_loader_needs_enough_days():
    def few_days(_day: date):
        return {"EUR": 0.9} if _day.day <= 2 else None

    assert fetch_month_average_rates("2026-08", day_loader=few_days) is None

    def enough(_day: date):
        return {"EUR": 0.9, "UAH": 40.0}

    result = fetch_month_average_rates("2026-08", day_loader=enough)
    assert result is not None
    assert result["_days"] == 31
    assert result["EUR"] == 0.9
    assert valid_period("2026-13") is None


def test_store_uses_cached_month_average_until_overridden(tmp_path):
    calls = []

    def loader(period: str):
        calls.append(period)
        return {"EUR": 0.5, "USD": 1.0, "_days": 31}

    store = Store(tmp_path / "fx.db", fx_loader=loader)
    first = store.fx_rates_for("2026-08")
    second = store.fx_rates_for("2026-08")
    assert first["EUR"] == 0.5
    assert second["EUR"] == 0.5
    assert calls == ["2026-08"]
    assert to_usd(920, "EUR", first) == 1840.0

    store.set_fx_rates({"EUR": "0.4"}, period="2026-08")
    assert store.fx_snapshot("2026-08")["overridden"] is True
    assert store.fx_rates_for("2026-08")["EUR"] == 0.4

    refreshed = store.fx_snapshot("2026-08", refresh=True)
    assert refreshed["overridden"] is False
    assert refreshed["rates"]["EUR"] == 0.5
    assert calls == ["2026-08", "2026-08"]


def test_reporting_month_rates_load_once_when_the_month_closes(tmp_path):
    calls = []

    def loader(period: str):
        calls.append(period)
        return {"EUR": 0.81, "USD": 1.0, "_days": 31}

    store = Store(tmp_path / "fx-auto.db", fx_loader=loader)
    assert store.autoload_reporting_rates(date(2026, 9, 1)) is True
    assert calls == ["2026-08"]
    assert store.fx_rates_for("2026-08")["EUR"] == 0.81
    assert store.autoload_reporting_rates(date(2026, 9, 1)) is False
    assert store.autoload_reporting_rates(date(2026, 9, 15)) is False
    assert calls == ["2026-08"]

    store.set_fx_rates({"EUR": "0.5"}, period="2026-09")
    assert store.autoload_reporting_rates(date(2026, 10, 1)) is False
    assert store.fx_rates_for("2026-09")["EUR"] == 0.5
    assert calls == ["2026-08"]

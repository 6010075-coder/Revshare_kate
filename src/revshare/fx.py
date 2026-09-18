from __future__ import annotations

import json
import re
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SUPPORTED_CURRENCIES = (
    "USD",
    "EUR",
    "UAH",
    "AED",
    "AMD",
    "KZT",
    "RUB",
    "TRY",
)

DEFAULT_USD_RATES = {
    "USD": 1.0,
    "EUR": 0.92,
    "UAH": 41.5,
    "AED": 3.67,
    "AMD": 385.0,
    "KZT": 520.0,
    "RUB": 90.0,
    "TRY": 41.0,
}

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
FX_SOURCE = "currency-api"
_USER_AGENT = "integration-hub-revshare/1.0"


def to_usd(amount: float | None, currency: str, rates: dict[str, float]) -> float | None:
    if amount is None:
        return None
    code = (currency or "").upper()
    if code == "USD":
        return round(float(amount), 2)
    rate = rates.get(code)
    if not rate:
        return None
    return round(float(amount) / rate, 2)


def valid_period(value: str | None) -> str | None:
    if not value or not PERIOD_RE.match(value):
        return None
    year, month = (int(part) for part in value.split("-"))
    if month < 1 or month > 12:
        return None
    return f"{year:04d}-{month:02d}"


def month_days(period: str) -> list[date]:
    checked = valid_period(period)
    if not checked:
        return []
    year, month = (int(part) for part in checked.split("-"))
    last = monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last + 1)]


def average_usd_rates(daily: list[dict[str, float]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for quotes in daily:
        for code, value in quotes.items():
            currency = code.upper()
            if currency not in SUPPORTED_CURRENCIES or currency == "USD":
                continue
            try:
                rate = float(value)
            except (TypeError, ValueError):
                continue
            if rate <= 0:
                continue
            totals[currency] = totals.get(currency, 0.0) + rate
            counts[currency] = counts.get(currency, 0) + 1
    return {
        currency: round(totals[currency] / counts[currency], 6)
        for currency in totals
        if counts[currency]
    }


def _read_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_usd_rates_for_day(day: date) -> dict[str, float] | None:
    urls = (
        f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{day.isoformat()}/v1/currencies/usd.min.json",
        f"https://{day.isoformat()}.currency-api.pages.dev/v1/currencies/usd.min.json",
    )
    payload = None
    for url in urls:
        try:
            payload = _read_json(url)
            break
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    if not isinstance(payload, dict):
        return None
    quotes = payload.get("usd")
    if not isinstance(quotes, dict):
        return None
    rates = {}
    for code in SUPPORTED_CURRENCIES:
        if code == "USD":
            continue
        value = quotes.get(code.lower())
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            rates[code] = rate
    return rates or None


def fetch_month_average_rates(
    period: str,
    *,
    day_loader: Callable[[date], dict[str, float] | None] | None = None,
) -> dict[str, float] | None:
    days = month_days(period)
    if not days:
        return None
    loader = day_loader or fetch_usd_rates_for_day
    collected: list[dict[str, float]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(loader, day): day for day in days}
        for future in as_completed(futures):
            quotes = future.result()
            if quotes:
                collected.append(quotes)
    if len(collected) < 5:
        return None
    averaged = average_usd_rates(collected)
    if not averaged:
        return None
    averaged["USD"] = 1.0
    averaged["_days"] = float(len(collected))
    return averaged

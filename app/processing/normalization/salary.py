"""Salary parsing and normalization.

The rule that shapes this module: **never invent a salary**. If a posting does
not disclose compensation, the resulting :class:`SalaryInfo` stays empty with
``provenance = unknown``. Only figures actually written in the posting are
marked ``observed``, and cross-currency comparison uses a dated rate table that
is part of the configuration rather than a hard-coded guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.models.enums import ANNUALIZATION_FACTORS, SalaryPeriod, SalaryProvenance
from app.models.job import SalaryInfo

log = get_logger(__name__)

DEFAULT_RATES_PATH = Path("configs") / "currency_rates.yaml"

CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₺": "TRY",
    "₽": "RUB",
    "₩": "KRW",
    "R$": "BRL",
    "zł": "PLN",
    "Kč": "CZK",
    "CHF": "CHF",
    "﷼": "IRR",
}

CURRENCY_WORDS: dict[str, str] = {
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "us dollar": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "sterling": "GBP",
    "chf": "CHF",
    "franc": "CHF",
    "sek": "SEK",
    "nok": "NOK",
    "dkk": "DKK",
    "pln": "PLN",
    "czk": "CZK",
    "huf": "HUF",
    "ron": "RON",
    "bgn": "BGN",
    "try": "TRY",
    "uah": "UAH",
    "cad": "CAD",
    "aud": "AUD",
    "nzd": "NZD",
    "jpy": "JPY",
    "cny": "CNY",
    "rmb": "CNY",
    "hkd": "HKD",
    "sgd": "SGD",
    "krw": "KRW",
    "twd": "TWD",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "aed": "AED",
    "dirham": "AED",
    "sar": "SAR",
    "qar": "QAR",
    "ils": "ILS",
    "shekel": "ILS",
    "egp": "EGP",
    "zar": "ZAR",
    "rand": "ZAR",
    "ngn": "NGN",
    "brl": "BRL",
    "real": "BRL",
    "mxn": "MXN",
    "peso": "MXN",
    "myr": "MYR",
    "thb": "THB",
    "php": "PHP",
    "idr": "IDR",
    "vnd": "VND",
    "pkr": "PKR",
    "irr": "IRR",
    "toman": "IRR",
}

PERIOD_WORDS: tuple[tuple[SalaryPeriod, tuple[str, ...]], ...] = (
    (SalaryPeriod.HOURLY, ("per hour", "an hour", "/hour", "/hr", "hourly", "p/h", "per hr")),
    (SalaryPeriod.DAILY, ("per day", "a day", "/day", "daily", "per diem", "day rate")),
    (SalaryPeriod.WEEKLY, ("per week", "a week", "/week", "weekly")),
    (
        SalaryPeriod.MONTHLY,
        ("per month", "a month", "/month", "/mo", "monthly", "pro monat", "brutto/monat"),
    ),
    (
        SalaryPeriod.YEARLY,
        (
            "per year",
            "a year",
            "/year",
            "/yr",
            "yearly",
            "annual",
            "annually",
            "per annum",
            "p.a.",
            "pa",
            "jahr",
            "brutto/jahr",
            "per ann",
        ),
    ),
)

#: Salaries below/above these annualised bounds are almost certainly a parsing
#: error (a phone number, an employee count, a stock price).
MIN_PLAUSIBLE_ANNUAL = 3_000.0
MAX_PLAUSIBLE_ANNUAL = 2_000_000.0

_AMOUNT_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:[.,\u00a0\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*([kKmM])?(?![\w])"
)
_RANGE_HINT_RE = re.compile(r"\b(?:to|bis|until|a)\b|[-\u2013\u2014~]|\.{2,}")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass(frozen=True, slots=True)
class CurrencyTable:
    """A dated snapshot of exchange rates."""

    base: str
    as_of: date | None
    rates: dict[str, float]
    source: str = ""

    def convert(self, amount: float | None, currency: str | None) -> float | None:
        """Convert into the base currency, or ``None`` if the rate is unknown."""
        if amount is None or not currency:
            return None
        code = currency.upper()
        if code == self.base:
            return amount
        rate = self.rates.get(code)
        if rate is None or rate <= 0:
            return None
        return amount * rate

    def supports(self, currency: str | None) -> bool:
        return currency is not None and currency.upper() in self.rates


@lru_cache(maxsize=4)
def load_currency_table(path: str | None = None) -> CurrencyTable:
    """Load the exchange-rate snapshot from configuration."""
    config_path = Path(path) if path else DEFAULT_RATES_PATH
    if not config_path.exists():
        log.warning("salary.rates_missing", path=str(config_path))
        return CurrencyTable(base="USD", as_of=None, rates={"USD": 1.0})

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    rates = payload.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise ConfigurationError(f"{config_path} does not define any rates")

    parsed: dict[str, float] = {}
    for code, value in rates.items():
        try:
            parsed[str(code).upper()] = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid rate for {code!r} in {config_path}") from exc

    as_of_raw = payload.get("as_of")
    as_of: date | None = None
    if as_of_raw:
        as_of = as_of_raw if isinstance(as_of_raw, date) else date.fromisoformat(str(as_of_raw))

    return CurrencyTable(
        base=str(payload.get("base", "USD")).upper(),
        as_of=as_of,
        rates=parsed,
        source=str(payload.get("source", "")),
    )


def parse_amount(token: str, suffix: str | None) -> float | None:
    """Turn ``"60.000"``, ``"60,000"``, ``"60k"`` or ``"1.2M"`` into a number."""
    text = token.replace("\u00a0", "").replace(" ", "")
    if not text:
        return None

    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # The right-most separator is the decimal one.
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        tail = text.rsplit(sep, 1)[-1]
        if len(tail) == 3 and text.count(sep) >= 1 and len(text.replace(sep, "")) > 3:
            text = text.replace(sep, "")
        else:
            text = text.replace(sep, ".")

    try:
        value = float(text)
    except ValueError:
        return None

    if suffix:
        value *= 1_000 if suffix.lower() == "k" else 1_000_000
    return value


def detect_currency(text: str) -> str | None:
    """Find the currency of a salary string."""
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    lowered = f" {text.lower()} "
    for word, code in CURRENCY_WORDS.items():
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered):
            return code
    return None


def detect_period(text: str) -> SalaryPeriod:
    """Find the payment cadence of a salary string."""
    lowered = text.lower()
    for period, keywords in PERIOD_WORDS:
        if any(keyword in lowered for keyword in keywords):
            return period
    return SalaryPeriod.UNKNOWN


class SalaryParser:
    """Extracts a salary range from free text."""

    def __init__(self, *, currency_table: CurrencyTable | None = None) -> None:
        self._table = currency_table or load_currency_table()

    @property
    def base_currency(self) -> str:
        return self._table.base

    def parse(
        self,
        text: str | None,
        *,
        country_code: str | None = None,
        fallback_currency: str | None = None,
    ) -> SalaryInfo:
        """Parse a salary string. Returns an empty result when nothing is stated."""
        if not text or not text.strip():
            return SalaryInfo(provenance=SalaryProvenance.UNKNOWN)

        raw = text.strip()[:256]
        cleaned = _YEAR_RE.sub(" ", raw)
        currency = detect_currency(raw) or fallback_currency or _currency_for_country(country_code)
        period = detect_period(raw)

        amounts: list[float] = []
        for match in _AMOUNT_RE.finditer(cleaned):
            value = parse_amount(match.group(1), match.group(2))
            if value is not None and value > 0:
                amounts.append(value)
        if not amounts:
            return SalaryInfo(raw=raw, provenance=SalaryProvenance.UNKNOWN)

        amounts = amounts[:4]
        low = min(amounts)
        high = max(amounts) if len(amounts) > 1 and _RANGE_HINT_RE.search(cleaned) else None
        if high is None and len(amounts) > 1:
            high = max(amounts)

        period = period if period is not SalaryPeriod.UNKNOWN else self._infer_period(low)
        confidence = self._confidence(currency, period, low, high, raw)

        info = SalaryInfo(
            raw=raw,
            min_amount=low,
            max_amount=high,
            currency=currency,
            period=period,
            provenance=SalaryProvenance.OBSERVED,
            confidence=confidence,
        )
        annual_min, annual_max = info.annualize()
        normalized_min = (
            self._table.convert(annual_min, currency) if annual_min and currency else None
        )
        normalized_max = (
            self._table.convert(annual_max, currency) if annual_max and currency else None
        )
        converted = normalized_min is not None or normalized_max is not None

        # Plausibility is judged in the reference currency whenever we have a
        # rate: 5,000,000 JPY a year is an ordinary salary, 5,000,000 USD is not.
        if converted:
            plausible = self._is_plausible(
                normalized_min, normalized_max, MIN_PLAUSIBLE_ANNUAL, MAX_PLAUSIBLE_ANNUAL
            )
        else:
            plausible = self._is_plausible(annual_min, annual_max, 100.0, 1e11)
        if not plausible:
            # The numbers parsed, but they cannot be a salary.
            return SalaryInfo(raw=raw, provenance=SalaryProvenance.UNKNOWN, confidence=0.0)

        return info.model_copy(
            update={
                "annual_min": normalized_min if normalized_min is not None else annual_min,
                "annual_max": normalized_max if normalized_max is not None else annual_max,
                "normalized_currency": self._table.base if converted else currency,
            }
        )

    @staticmethod
    def _infer_period(amount: float) -> SalaryPeriod:
        """Guess the cadence from the magnitude when the text does not say."""
        if amount < 500:
            return SalaryPeriod.HOURLY
        if amount < 15_000:
            return SalaryPeriod.MONTHLY
        return SalaryPeriod.YEARLY

    @staticmethod
    def _is_plausible(
        annual_min: float | None, annual_max: float | None, low: float, high: float
    ) -> bool:
        values = [v for v in (annual_min, annual_max) if v is not None]
        if not values:
            return False
        return all(low <= v <= high for v in values)

    @staticmethod
    def _confidence(
        currency: str | None,
        period: SalaryPeriod,
        low: float,
        high: float | None,
        raw: str,
    ) -> float:
        score = 0.35
        if currency:
            score += 0.25
        if period in ANNUALIZATION_FACTORS:
            score += 0.2
        if high is not None and high > low:
            score += 0.15
        if len(raw) <= 80:
            score += 0.05
        return round(min(score, 1.0), 3)


#: Fallback currency when a posting states an amount without a currency but we
#: know the country. Only used for countries with a single obvious currency.
_COUNTRY_CURRENCY: dict[str, str] = {
    "US": "USD",
    "GB": "GBP",
    "CH": "CHF",
    "SE": "SEK",
    "NO": "NOK",
    "DK": "DKK",
    "PL": "PLN",
    "CZ": "CZK",
    "HU": "HUF",
    "RO": "RON",
    "TR": "TRY",
    "UA": "UAH",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
    "JP": "JPY",
    "CN": "CNY",
    "HK": "HKD",
    "SG": "SGD",
    "KR": "KRW",
    "TW": "TWD",
    "IN": "INR",
    "AE": "AED",
    "SA": "SAR",
    "QA": "QAR",
    "IL": "ILS",
    "EG": "EGP",
    "ZA": "ZAR",
    "NG": "NGN",
    "BR": "BRL",
    "MX": "MXN",
    "MY": "MYR",
    "TH": "THB",
    "PH": "PHP",
    "ID": "IDR",
    "VN": "VND",
    "PK": "PKR",
    "IR": "IRR",
    "DE": "EUR",
    "FR": "EUR",
    "NL": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "PT": "EUR",
    "IE": "EUR",
    "AT": "EUR",
    "BE": "EUR",
    "FI": "EUR",
    "GR": "EUR",
    "SK": "EUR",
    "SI": "EUR",
    "EE": "EUR",
    "LV": "EUR",
    "LT": "EUR",
    "LU": "EUR",
    "HR": "EUR",
}


def _currency_for_country(country_code: str | None) -> str | None:
    return _COUNTRY_CURRENCY.get(country_code.upper()) if country_code else None


__all__ = [
    "CURRENCY_SYMBOLS",
    "CURRENCY_WORDS",
    "MAX_PLAUSIBLE_ANNUAL",
    "MIN_PLAUSIBLE_ANNUAL",
    "PERIOD_WORDS",
    "CurrencyTable",
    "SalaryParser",
    "detect_currency",
    "detect_period",
    "load_currency_table",
    "parse_amount",
]

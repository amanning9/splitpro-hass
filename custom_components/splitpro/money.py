"""Currencies and categories, copied from Split Pro (src/lib/currency.ts and src/lib/category.ts).

Split Pro stores every amount as an integer in the currency's smallest unit (pence for GBP), and
its API neither lists currencies nor validates them, so the integration has to know the decimal
places itself.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Currencies whose smallest unit isn't 1/100 of the main unit.
_ZERO_DECIMALS = {
    "AFN", "ALL", "AMD", "BIF", "CLP", "COP", "CRC", "DJF", "GNF", "HUF", "IDR", "IQD", "IRR",
    "ISK", "JPY", "KMF", "KRW", "LBP", "MGA", "MMK", "MUR", "PKR", "PYG", "RSD", "RWF", "SOS",
    "SYP", "TZS", "UGX", "UZS", "VND", "XAF", "XOF", "YER", "ZMK", "ZWL",
}
_THREE_DECIMALS = {"BHD", "JOD", "KWD", "LYD", "OMR", "TND"}
_TWO_DECIMALS = {
    "AED", "ARS", "AUD", "AZN", "BAM", "BDT", "BGN", "BND", "BOB", "BRL", "BWP", "BYN", "BZD",
    "CAD", "CDF", "CHF", "CNY", "CVE", "CZK", "DKK", "DOP", "DZD", "EEK", "EGP", "ERN", "ETB",
    "EUR", "GBP", "GEL", "GHS", "GTQ", "HKD", "HNL", "HRK", "ILS", "INR", "JMD", "KES", "KHR",
    "KZT", "LKR", "LTL", "LVL", "MAD", "MDL", "MKD", "MOP", "MXN", "MYR", "MZN", "NAD", "NGN",
    "NIO", "NOK", "NPR", "NZD", "PAB", "PEN", "PHP", "PLN", "QAR", "RON", "RUB", "SAR", "SDG",
    "SEK", "SGD", "THB", "TOP", "TRY", "TTD", "TWD", "UAH", "USD", "UYU", "VEF", "ZAR",
}
CURRENCIES = frozenset(_ZERO_DECIMALS | _THREE_DECIMALS | _TWO_DECIMALS)

# The integration's last-resort currency, as on the alex-hacks Split Pro build.
DEFAULT_CURRENCY = "GBP"

# Split Pro's categories: a section name or one of its items ("other" isn't a valid value).
CATEGORIES = (
    "general",
    "entertainment", "games", "movies", "music", "sports",
    "food", "diningOut", "groceries", "liquor",
    "home", "electronics", "furniture", "supplies", "maintenance", "mortgage", "pets", "rent",
    "services",
    "life", "childcare", "clothing", "education", "gifts", "insurance", "medical", "taxes",
    "travel", "bicycle", "bus", "train", "car", "fuel", "hotel", "parking", "plane", "taxi",
    "utilities", "cleaning", "electricity", "gas", "internet", "trash", "phone", "water",
)
DEFAULT_CATEGORY = "general"


def is_currency(code: str | None) -> bool:
    """Return True for a currency code Split Pro knows."""
    return code in CURRENCIES


def decimal_digits(currency: str) -> int:
    """Number of decimal places of the currency's smallest unit."""
    if currency in _ZERO_DECIMALS:
        return 0
    if currency in _THREE_DECIMALS:
        return 3
    return 2


def to_minor_units(amount: Decimal | float | int | str, currency: str) -> int:
    """Convert e.g. 12.34 GBP to 1234. Raises ValueError if it has more decimals than the currency."""
    value = Decimal(str(amount))
    scaled = value.scaleb(decimal_digits(currency))
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} has too many decimal places for {currency}")
    return int(scaled)


def from_minor_units(amount: int, currency: str) -> float:
    """Convert e.g. 1234 pence to 12.34 (for sensor states)."""
    digits = decimal_digits(currency)
    return float((Decimal(amount).scaleb(-digits)).quantize(Decimal(1).scaleb(-digits), ROUND_HALF_UP))

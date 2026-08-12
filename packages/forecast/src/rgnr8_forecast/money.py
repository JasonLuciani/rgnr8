"""Exact money in integer minor units.

Mirrors the accounting kernel's money model: values are an integer count of a
currency's minor unit (e.g. cents). Python ints are arbitrary precision, so all
arithmetic here is exact and reproducible — never binary floating point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

_SCALES: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2}


def currency_scale(code: str) -> int:
    """Minor-unit digits for a currency code (2 for USD)."""
    try:
        return _SCALES[code]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Unknown currency: {code}") from exc


class CurrencyMismatch(ValueError):
    pass


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact monetary amount as integer minor units plus a currency code.

    ``order=True`` compares by (minor_units, currency) — comparisons between
    different currencies are rejected explicitly by the arithmetic helpers, but
    the dataclass ordering is only meaningful within one currency.
    """

    minor_units: int
    currency: str = "USD"

    # --- constructors --------------------------------------------------------
    @staticmethod
    def zero(currency: str = "USD") -> "Money":
        return Money(0, currency)

    @staticmethod
    def from_decimal(value: str | int, currency: str = "USD") -> "Money":
        """Parse an exact decimal string (e.g. ``"1234.56"``) into minor units.

        Rejects more fractional digits than the currency allows rather than
        silently rounding — precision loss must be an explicit caller decision.
        """
        scale = currency_scale(currency)
        s = str(value).strip()
        neg = s.startswith("-")
        if neg:
            s = s[1:]
        if "." in s:
            whole, frac = s.split(".", 1)
        else:
            whole, frac = s, ""
        if not whole.isdigit() or (frac and not frac.isdigit()):
            raise ValueError(f"Not a valid decimal amount: {value!r}")
        if len(frac) > scale:
            raise ValueError(
                f"Amount {value!r} has more precision than {currency} allows (scale {scale})"
            )
        frac = frac.ljust(scale, "0")
        minor = int(whole or "0") * (10**scale) + int(frac or "0")
        return Money(-minor if neg else minor, currency)

    # --- arithmetic ----------------------------------------------------------
    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor_units, self.currency)

    def scale_by(self, numerator: int, denominator: int) -> "Money":
        """Multiply by an exact rational ``numerator/denominator`` with banker's
        rounding to the nearest minor unit. Used for haircuts and fractions."""
        if denominator == 0:
            raise ValueError("denominator must be non-zero")
        num = self.minor_units * numerator
        den = denominator
        # Normalise the sign onto the numerator so the denominator is positive.
        # `divmod` then floors toward -inf with 0 <= r < den, so `q` is always the
        # lower integer and rounding up is unconditionally `q += 1` — regardless of
        # the value's sign. (The previous `q -= 1` branch double-rounded negatives.)
        if den < 0:
            num, den = -num, -den
        q, r = divmod(num, den)
        twice = r * 2
        # round half to even
        if twice > den or (twice == den and q % 2 != 0):
            q += 1
        return Money(q, self.currency)

    def abs(self) -> "Money":
        return Money(abs(self.minor_units), self.currency)

    # --- predicates ----------------------------------------------------------
    @property
    def is_zero(self) -> bool:
        return self.minor_units == 0

    @property
    def is_positive(self) -> bool:
        return self.minor_units > 0

    @property
    def is_negative(self) -> bool:
        return self.minor_units < 0

    # --- formatting ----------------------------------------------------------
    def to_decimal_string(self) -> str:
        scale = currency_scale(self.currency)
        neg = self.minor_units < 0
        s = str(abs(self.minor_units)).rjust(scale + 1, "0")
        if scale == 0:
            body = s
        else:
            body = f"{s[:-scale]}.{s[-scale:]}"
        return f"-{body}" if neg else body

    def __str__(self) -> str:
        return f"{self.currency} {self.to_decimal_string()}"


def money_sum(items: Iterable[Money], currency: str = "USD") -> Money:
    total = Money.zero(currency)
    for m in items:
        total = total + m
    return total

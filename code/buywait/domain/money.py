"""Exact monetary value object and deterministic formatting primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Self

from .enums import Currency
from .errors import InvalidInputError, UnsupportedCurrencyError


MINOR_UNIT = Decimal("0.01")


def parse_decimal(value: str | int | Decimal) -> Decimal:
    if isinstance(value, float):
        raise InvalidInputError("Binary floating-point values are forbidden for money.")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise InvalidInputError("Invalid decimal amount.", context={"value": str(value)}) from error
    if not parsed.is_finite():
        raise InvalidInputError("Money must be finite.", context={"value": str(value)})
    return Decimal(0) if parsed == 0 else parsed


def exact_decimal_text(value: Decimal) -> str:
    """Return a non-scientific, lossless decimal string."""
    text = format(value, "f")
    return "0" if Decimal(text) == 0 else text


def trimmed_decimal_text(value: Decimal) -> str:
    text = exact_decimal_text(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def floor_to_minor_unit(value: Decimal) -> Decimal:
    return value.quantize(MINOR_UNIT, rounding=ROUND_FLOOR)


def round_to_minor_unit(value: Decimal) -> Decimal:
    return value.quantize(MINOR_UNIT, rounding=ROUND_HALF_UP)


def format_safe_amount(value: Decimal) -> str:
    """Never round financial capacity upward; omit insignificant zeros."""
    return trimmed_decimal_text(floor_to_minor_unit(value))


def format_plan_amount(value: Decimal) -> str:
    """Show integers without decimals and fractional plan payments to two places."""
    rounded = round_to_minor_unit(value)
    if rounded == rounded.to_integral_value():
        return format(rounded.quantize(Decimal("1")), "f")
    return format(rounded, ".2f")


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if isinstance(self.amount, float):
            raise InvalidInputError("Binary floating-point values are forbidden for money.")
        parsed = parse_decimal(self.amount)
        object.__setattr__(self, "amount", parsed)
        if not isinstance(self.currency, Currency):
            try:
                object.__setattr__(self, "currency", Currency(str(self.currency)))
            except ValueError as error:
                raise UnsupportedCurrencyError(
                    "Unsupported currency.", context={"currency": str(self.currency)}
                ) from error

    @classmethod
    def parse(cls, amount: str | int | Decimal, currency: str | Currency) -> Self:
        try:
            parsed_currency = currency if isinstance(currency, Currency) else Currency(currency)
        except ValueError as error:
            raise UnsupportedCurrencyError(
                "Unsupported currency.", context={"currency": str(currency)}
            ) from error
        return cls(parse_decimal(amount), parsed_currency)

    @classmethod
    def zero(cls, currency: Currency) -> Self:
        return cls(Decimal(0), currency)

    def _require_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise InvalidInputError(
                "Currency mismatch.",
                context={"left": self.currency.value, "right": other.currency.value},
            )

    def __add__(self, other: Money) -> Self:
        self._require_currency(other)
        return type(self)(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Self:
        self._require_currency(other)
        return type(self)(self.amount - other.amount, self.currency)

    def negate(self) -> Self:
        return type(self)(-self.amount, self.currency)


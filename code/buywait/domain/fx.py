"""Dated, directional, offline FX with exact decimal multiplication."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from types import MappingProxyType

from .enums import Currency
from .errors import InvalidInputError, MissingExchangeRateError
from .models import ExchangeRate
from .money import Money

RateKey = tuple[date, Currency, Currency]


def exact_product(left: Decimal, right: Decimal) -> Decimal:
    # Multiplication needs at most the sum of the operands' coefficient digits.
    with localcontext() as context:
        context.prec = max(28, len(left.as_tuple().digits) + len(right.as_tuple().digits))
        return left * right


@dataclass(frozen=True, slots=True, init=False)
class RateBook:
    rates: Mapping[RateKey, ExchangeRate]

    def __init__(self, rates: Iterable[ExchangeRate]) -> None:
        index: dict[RateKey, ExchangeRate] = {}
        for rate in rates:
            key = rate.rate_date, rate.from_currency, rate.to_currency
            if key in index:
                raise InvalidInputError("Duplicate dated exchange-rate key.")
            index[key] = rate
        object.__setattr__(self, "rates", MappingProxyType(index))

    def lookup(self, rate_date: date, from_currency: Currency, to_currency: Currency) -> ExchangeRate:
        if from_currency == to_currency:
            return ExchangeRate(rate_date, from_currency, to_currency, Decimal(1))
        key = rate_date, from_currency, to_currency
        try:
            return self.rates[key]
        except KeyError as error:
            raise MissingExchangeRateError(
                "No supplied exchange rate for the exact date and direction.",
                context={"rate_date": rate_date.isoformat(), "from_currency": from_currency.value,
                         "to_currency": to_currency.value},
            ) from error

    def convert(self, amount: Money, to_currency: Currency, rate_date: date) -> Money:
        rate = self.lookup(rate_date, amount.currency, to_currency)
        return Money(exact_product(amount.amount, rate.rate), to_currency)

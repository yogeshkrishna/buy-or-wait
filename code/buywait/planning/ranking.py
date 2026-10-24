"""The challenge's deterministic lexicographic candidate ranking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

from buywait.domain.models import CandidatePlan


@dataclass(frozen=True, order=True, slots=True)
class RankKey:
    completion_penalty: int
    change_penalty: int
    total_payable: Decimal
    first_payment_date: date
    payment_count: int
    payment_option_order: int

    def as_tuple(self) -> tuple:
        return (
            self.completion_penalty,
            self.change_penalty,
            self.total_payable,
            self.first_payment_date,
            self.payment_count,
            self.payment_option_order,
        )


def _option_order(value: str | None) -> int:
    if value is None:
        return -1
    match = re.search(r"(\d+)$", value)
    return int(match.group(1)) if match else 2**31 - 1


def rank_key(candidate: CandidatePlan, desired_completion_date: date) -> RankKey:
    completion = candidate.completion_date or date.max
    first = candidate.payments[0].payment_date if candidate.payments else date.max
    return RankKey(
        completion_penalty=0 if completion <= desired_completion_date else 1,
        change_penalty=0 if not candidate.spending_changes else 1,
        total_payable=candidate.total_payable_amount.amount,
        first_payment_date=first,
        payment_count=len(candidate.payments),
        payment_option_order=_option_order(candidate.payment_option_id),
    )


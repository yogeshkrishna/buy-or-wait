"""Evidence-bounded recurrence inference from canonical historical cash flows."""

from __future__ import annotations

import calendar
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re

from buywait.domain.enums import (
    AmountStrategy,
    Direction,
    EvidenceFactType,
    Flexibility,
    RecurrenceCadence,
)
from buywait.domain.models import CanonicalCashFlow, CanonicalLedger, DateRange, EvidenceFact, RecurrenceRule
from buywait.domain.money import Money


_ONE_TIME_INCOME = re.compile(
    r"\b(bonus|commission|arrears|prize|lottery|reimbursement|refund|payout|adjustment)\b",
    re.IGNORECASE,
)
_SALARY_WORDS = re.compile(r"\b(salary|payroll|wage|gaji)\b", re.IGNORECASE)
_ENDED_INCOME = re.compile(
    r"\b(final|last)\s+(?:employer\s+)?(?:salary|payroll|wage|payment)\b|"
    r"\b(?:salary|payroll|employment|contract|seasonal\s+work)\s+(?:has\s+)?(?:ended|finished|stopped)\b",
    re.IGNORECASE,
)
_VARIABLE_ESSENTIAL_CATEGORIES = frozenset({"groceries", "healthcare", "transport", "utilities"})


def _month_index(value: date) -> int:
    return value.year * 12 + value.month


def _is_month_end(value: date) -> bool:
    return value.day == calendar.monthrange(value.year, value.month)[1]


def _add_months(value: date, months: int, *, day_of_month: int | None, end_of_month: bool) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    last = calendar.monthrange(year, month)[1]
    day = last if end_of_month else min(day_of_month or value.day, last)
    return date(year, month, day)


def _nearest_rank(values: list[Decimal], percentile: Decimal) -> Decimal:
    ordered = sorted(values)
    rank = int((Decimal(len(ordered)) * percentile).to_integral_value(rounding=ROUND_CEILING))
    return ordered[max(1, rank) - 1]


class RecurrenceDetector:
    """Infer only strongly evidenced cash recurrences; never invent a one-row rule."""

    def __init__(
        self,
        *,
        minimum_observations: int = 3,
        recent_window: int = 6,
        variable_essential_estimator: str = "nearest_rank_p75",
        other_recurring_debit_estimator: str = "latest",
    ) -> None:
        self.minimum_observations = minimum_observations
        self.recent_window = recent_window
        self.variable_essential_estimator = variable_essential_estimator
        self.other_recurring_debit_estimator = other_recurring_debit_estimator

    def infer(
        self,
        ledger: CanonicalLedger,
        facts: tuple[EvidenceFact, ...] | list[EvidenceFact] = (),
    ) -> tuple[RecurrenceRule, ...]:
        by_flow = {flow.flow_id: flow for flow in ledger.historical_flows}
        rules: list[RecurrenceRule] = []
        covered_semantics: set[str] = set()
        for candidate in ledger.historical_series_candidates:
            members = [by_flow[item] for item in candidate.source_flow_ids if item in by_flow]
            semantic_groups = self._semantic_groups(candidate.category, candidate.direction, members)
            expanded_groups = {
                stream_key: stream
                for semantic_key, observations in semantic_groups.items()
                for stream_key, stream in self._calendar_streams(semantic_key, observations).items()
            }
            for semantic_key, observations in expanded_groups.items():
                observations.sort(key=lambda item: (item.effective_date, item.flow_id))
                cadence_observations = list(observations)
                # A dated, confirmed upcoming payroll is a third independent
                # cadence point. It supports continuation but does not redefine
                # the stable base amount when evidence says "next cycle" only.
                if candidate.direction is Direction.CREDIT and candidate.category.lower() == "salary":
                    future_salary = [
                        flow for flow in ledger.forecast_flows
                        if flow.direction is Direction.CREDIT and flow.category.lower() == "salary"
                    ]
                    if semantic_key.endswith(":secondary") or ":variable_contract" in semantic_key:
                        future_salary = []
                    cadence_observations.extend(future_salary)
                    cadence_observations.sort(key=lambda item: (item.effective_date, item.flow_id))
                    amount_observations = self._salary_amount_observations(observations, future_salary, facts)
                else:
                    amount_observations = observations
                rule = self._rule(
                    ledger, semantic_key, amount_observations, cadence_observations, facts,
                    explicitly_supported=(candidate.direction is Direction.CREDIT
                                          and candidate.category.lower() == "salary"
                                          and len(cadence_observations) >= 2
                                          and len(cadence_observations) > len(observations)),
                )
                if rule is not None:
                    rules.append(rule)
                    covered_semantics.add(semantic_key)

        # Phase 05 intentionally emits history candidates only after two clean
        # rows. One settled payroll plus one explicitly scheduled future payroll
        # is nevertheless a supported two-point recurrence, not a single-row
        # guess. This path is generic and never depends on request IDs.
        salary_key = "credit:salary:base"
        # The fallback exists only for a ledger with no inferred salary stream.
        # Checking the literal ``:base`` key duplicated primary/secondary
        # household income under a second synthetic base rule.
        if not any(key.startswith("credit:salary:") for key in covered_semantics):
            historical_salary = [
                flow for flow in ledger.historical_flows
                if flow.direction is Direction.CREDIT and flow.category.lower() == "salary"
                and not _ONE_TIME_INCOME.search(flow.description)
            ]
            future_salary = [
                flow for flow in ledger.forecast_flows
                if flow.direction is Direction.CREDIT and flow.category.lower() == "salary"
            ]
            cadence_observations = sorted((*historical_salary, *future_salary),
                                          key=lambda item: (item.effective_date, item.flow_id))
            if historical_salary and future_salary:
                rule = self._rule(
                    ledger,
                    salary_key,
                    self._salary_amount_observations(historical_salary, future_salary, facts),
                    cadence_observations,
                    facts,
                    explicitly_supported=True,
                )
                if rule is not None:
                    rules.append(rule)
        return tuple(sorted(rules, key=lambda item: (item.direction.value, item.category, item.semantic_key, item.rule_id)))

    @staticmethod
    def _semantic_groups(category: str, direction: Direction, flows: list[CanonicalCashFlow]):
        groups: dict[str, list[CanonicalCashFlow]] = {}
        normalized_category = re.sub(r"\s+", "_", category.strip().lower())
        for flow in flows:
            # Variable essentials often use changing merchant descriptions. Their
            # stable financial identity is category + direction, not free text.
            key = f"{direction.value}:{normalized_category}"
            if direction is Direction.CREDIT and normalized_category == "salary":
                if _ONE_TIME_INCOME.search(flow.description):
                    continue
                description = flow.description.lower()
                if "primary household" in description:
                    subtype = "primary"
                elif "second household" in description:
                    subtype = "secondary"
                elif any(token in description for token in (
                    "freelance", "contract payment", "consulting", "independent work",
                    "design contract", "application project", "website project", "client retainer",
                )):
                    subtype = "variable_contract"
                else:
                    subtype = "base"
                key += f":{subtype}"
            elif direction is Direction.CREDIT and _ONE_TIME_INCOME.search(flow.description):
                continue
            groups.setdefault(key, []).append(flow)
        return groups

    @staticmethod
    def _calendar_streams(semantic_key: str, observations: list[CanonicalCashFlow]):
        """Split independently repeating semi-monthly income into monthly streams."""
        if not semantic_key.endswith(":variable_contract"):
            return {semantic_key: observations}
        by_day: dict[int, list[CanonicalCashFlow]] = {}
        for item in observations:
            by_day.setdefault(item.effective_date.day, []).append(item)
        supported = {day: items for day, items in by_day.items() if len(items) >= 3}
        if len(supported) < 2 or sum(map(len, supported.values())) < len(observations) * Decimal("0.75"):
            return {semantic_key: observations}
        return {f"{semantic_key}:day{day}": items for day, items in sorted(supported.items())}

    def _rule(
        self,
        ledger: CanonicalLedger,
        semantic_key: str,
        observations: list[CanonicalCashFlow],
        cadence_observations: list[CanonicalCashFlow],
        facts: tuple[EvidenceFact, ...] | list[EvidenceFact],
        *,
        explicitly_supported: bool = False,
    ) -> RecurrenceRule | None:
        required = 2 if explicitly_supported else self.minimum_observations
        if len(cadence_observations) < required:
            return None
        cadence = self._cadence(
            [item.effective_date for item in cadence_observations],
            minimum_points=required,
            allow_missing_cycles=explicitly_supported,
        )
        if cadence is None:
            return None
        (cadence_kind, interval, day_of_month, weekday, end_of_month,
         confidence, cadence_anchor, cadence_dates) = cadence
        latest = cadence_observations[-1]
        category = latest.category
        if self._cancelled(category, semantic_key, observations, facts, ledger.as_of_date):
            return None
        protected = any(item.protected for item in observations)
        flexibility = self._conservative_flexibility(item.flexibility for item in observations)
        aligned_amount_observations = [
            item for item in observations if item.effective_date in cadence_dates
        ] or observations
        if "salary" in semantic_key and self._has_next_cycle_salary_fact(facts):
            # A next-cycle amendment describes a temporary payroll amount. If
            # history contains a repeated stable salary, preserve that amount
            # for later cycles instead of extending the latest anomalous payslip
            # indefinitely (for example, one month affected by unpaid leave).
            dominant = self._dominant_amount(aligned_amount_observations)
        else:
            dominant = None
        if dominant is not None:
            strategy, amount = AmountStrategy.FIXED, dominant
        else:
            strategy, amount = self._amount(aligned_amount_observations, protected, category)
        minimums = [item.minimum_allowed_amount for item in observations if item.minimum_allowed_amount is not None]
        minimum = max(minimums, key=lambda item: item.amount) if minimums else None
        source_ids = tuple(sorted({source for item in cadence_observations for source in item.source_event_ids}))
        date_overrides = [
            fact for fact in facts
            if "salary" in semantic_key
            and fact.subject == "salary" and fact.scope == "recurring"
            and fact.fact_type in {EvidenceFactType.SETTLEMENT_DATE, EvidenceFactType.EFFECTIVE_DATE}
            and isinstance(fact.value, date)
        ]
        if date_overrides:
            chosen_date = max(date_overrides, key=lambda item: (
                item.provenance.observed_at.isoformat() if item.provenance.observed_at else "",
                item.value,
                item.fact_id,
            )).value
            cadence_anchor = chosen_date
            if cadence_kind is RecurrenceCadence.MONTHLY:
                end_of_month = _is_month_end(chosen_date)
                day_of_month = None if end_of_month else chosen_date.day
        relevant_facts = tuple(sorted({
            fact.fact_id for fact in facts
            if self._fact_matches(fact, category, semantic_key, source_ids)
        }))
        identity = json.dumps(
            [ledger.user_id, semantic_key, cadence_kind.value, interval, latest.effective_date.isoformat(), *source_ids],
            separators=(",", ":"),
        )
        return RecurrenceRule(
            rule_id="rule_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
            user_id=ledger.user_id,
            cadence=cadence_kind,
            interval=interval,
            amount_strategy=strategy,
            amount=Money(amount, ledger.home_currency),
            category=category,
            direction=latest.direction,
            source_event_ids=source_ids,
            active_range=DateRange(observations[0].effective_date, ledger.as_of_date + timedelta(days=90)),
            anchor_date=cadence_anchor,
            confidence=confidence,
            day_of_month=day_of_month,
            weekday=weekday,
            semantic_key=semantic_key,
            description=f"Inferred recurring {category}",
            protected=protected,
            flexibility=flexibility,
            minimum_allowed_amount=minimum,
            evidence_fact_ids=relevant_facts,
            end_of_month=end_of_month,
            observed_count=len(cadence_observations),
        )

    @staticmethod
    def _cadence(
        dates: list[date],
        *,
        minimum_points: int = 3,
        allow_missing_cycles: bool = False,
    ):
        unique = sorted(set(dates))
        if len(unique) < minimum_points:
            return None
        all_eom = all(_is_month_end(item) for item in unique)
        same_day = len({item.day for item in unique}) == 1
        monthly_members = unique
        if not (all_eom or same_day):
            position_counts = Counter("eom" if _is_month_end(item) else str(item.day) for item in unique)
            position, position_count = min(position_counts.items(), key=lambda item: (-item[1], item[0]))
            if (position_count >= minimum_points
                    and Decimal(position_count) / Decimal(len(unique)) >= Decimal("0.6")):
                monthly_members = [
                    item for item in unique
                    if (_is_month_end(item) if position == "eom" else item.day == int(position))
                ]
                all_eom = position == "eom"
                same_day = not all_eom
        month_gaps = [
            _month_index(right) - _month_index(left)
            for left, right in zip(monthly_members, monthly_members[1:])
        ]
        if month_gaps and len(monthly_members) >= minimum_points and (all_eom or same_day):
            counts = Counter(month_gaps)
            interval, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
            minimum_ratio = 0.5 if allow_missing_cycles else 0.75
            if interval > 0 and count / len(month_gaps) >= minimum_ratio:
                confidence = Decimal(count) / Decimal(len(month_gaps))
                return (
                    RecurrenceCadence.MONTHLY,
                    interval,
                    None if all_eom else monthly_members[-1].day,
                    None,
                    all_eom,
                    confidence,
                    monthly_members[-1],
                    frozenset(monthly_members),
                )
        gaps = [(right - left).days for left, right in zip(unique, unique[1:])]
        counts = Counter(gaps)
        gap, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
        if gap <= 0 or count / len(gaps) < 0.75:
            return None
        if gap == 1:
            kind, interval = RecurrenceCadence.DAILY, 1
        elif gap == 7:
            kind, interval = RecurrenceCadence.WEEKLY, 1
        elif gap == 14:
            kind, interval = RecurrenceCadence.BIWEEKLY, 1
        else:
            kind, interval = RecurrenceCadence.CUSTOM_DAYS, gap
        return (
            kind,
            interval,
            None,
            unique[-1].weekday(),
            False,
            Decimal(count) / Decimal(len(gaps)),
            unique[-1],
            frozenset(unique),
        )

    @staticmethod
    def _salary_amount_observations(historical, future, facts):
        next_cycle_ids = {
            fact.fact_id for fact in facts
            if fact.subject == "salary" and fact.scope == "next_cycle"
        }
        stable_future = [
            flow for flow in future
            if not (set(flow.evidence_fact_ids) & next_cycle_ids)
        ]
        return sorted((*historical, *stable_future), key=lambda item: (item.effective_date, item.flow_id))

    def _amount(self, observations: list[CanonicalCashFlow], protected: bool, category: str):
        recent = [item.amount.amount for item in observations[-self.recent_window:]]
        if len(set(recent)) == 1:
            return AmountStrategy.FIXED, recent[-1]
        if (observations[-1].direction is Direction.DEBIT
                and protected
                and category.strip().lower() in _VARIABLE_ESSENTIAL_CATEGORIES):
            return AmountStrategy.CONSERVATIVE_ROBUST, self._estimate(
                recent, self.variable_essential_estimator
            )
        if observations[-1].direction is Direction.DEBIT:
            amount = self._estimate(recent, self.other_recurring_debit_estimator)
            strategy = (AmountStrategy.LATEST
                        if self.other_recurring_debit_estimator == "latest"
                        else AmountStrategy.CONSERVATIVE_ROBUST)
            return strategy, amount
        return AmountStrategy.LATEST, recent[-1]

    @staticmethod
    def _estimate(values: list[Decimal], estimator: str) -> Decimal:
        if estimator == "nearest_rank_p75":
            return _nearest_rank(values, Decimal("0.75"))
        if estimator == "adaptive_median":
            percentile = Decimal("0.50") if len(values) >= 8 else Decimal("0.75")
            return _nearest_rank(values, percentile)
        if estimator == "nearest_rank_p50" or estimator == "median":
            return _nearest_rank(values, Decimal("0.50"))
        if estimator == "midpoint_median":
            ordered = sorted(values)
            middle = len(ordered) // 2
            if len(ordered) % 2:
                return ordered[middle]
            return (ordered[middle - 1] + ordered[middle]) / Decimal(2)
        if estimator == "mean":
            return sum(values, Decimal(0)) / Decimal(len(values))
        if estimator == "max":
            return max(values)
        if estimator == "latest":
            return values[-1]
        raise ValueError(f"unsupported variable essential estimator: {estimator}")

    @staticmethod
    def _has_next_cycle_salary_fact(facts: tuple[EvidenceFact, ...] | list[EvidenceFact]) -> bool:
        return any(fact.subject == "salary" and fact.scope == "next_cycle" for fact in facts)

    @staticmethod
    def _dominant_amount(observations: list[CanonicalCashFlow]) -> Decimal | None:
        counts = Counter(item.amount.amount for item in observations)
        if not counts:
            return None
        amount, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
        return amount if count >= 2 else None

    @staticmethod
    def _conservative_flexibility(values):
        values = set(values)
        if Flexibility.FIXED in values:
            return Flexibility.FIXED
        if Flexibility.REDUCIBLE in values and Flexibility.STOPPABLE in values:
            return Flexibility.REDUCIBLE_OR_STOPPABLE
        if Flexibility.REDUCIBLE_OR_STOPPABLE in values:
            return Flexibility.REDUCIBLE_OR_STOPPABLE
        if Flexibility.REDUCIBLE in values:
            return Flexibility.REDUCIBLE
        return Flexibility.STOPPABLE

    @staticmethod
    def _fact_matches(fact, category, semantic_key, source_ids):
        if fact.related_event_id in source_ids:
            return True
        subject = fact.subject.lower()
        category_text = category.lower()
        if "salary" in semantic_key:
            return subject == "salary"
        if subject == "rent":
            return "rent" in category_text or "housing" in category_text
        if subject == "childcare":
            return "childcare" in category_text
        if subject == "service":
            return any(token in category_text for token in ("subscription", "service", "membership"))
        return subject == category_text

    def _cancelled(self, category, semantic_key, observations, facts, as_of):
        if semantic_key.startswith("credit:salary") and any(
            _ENDED_INCOME.search(item.description) for item in observations
        ):
            return True
        source_ids = {source for item in observations for source in item.source_event_ids}
        for fact in facts:
            if fact.fact_type is not EvidenceFactType.CANCELLATION:
                continue
            if fact.effective_date is not None and fact.effective_date > as_of:
                continue
            if self._fact_matches(fact, category, semantic_key, source_ids):
                return True
        return False

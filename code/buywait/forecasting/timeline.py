"""Inclusive 90-day timeline composition and recurrence expansion."""

from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json

from buywait.domain.enums import CashState, Direction, EvidenceFactType, ForecastOrigin, RecurrenceCadence
from buywait.domain.models import (
    CanonicalCashFlow,
    CanonicalLedger,
    DateRange,
    EvidenceFact,
    ForecastOccurrence,
    ForecastTimeline,
    RecurrenceRule,
)
from buywait.domain.money import Money

from .recurrence import RecurrenceDetector


def _add_months(anchor: date, months: int, *, day_of_month: int | None, end_of_month: bool) -> date:
    index = anchor.year * 12 + anchor.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    last = calendar.monthrange(year, month)[1]
    day = last if end_of_month else min(day_of_month or anchor.day, last)
    return date(year, month, day)


class ForecastTimelineBuilder:
    HORIZON_DAYS = 90

    def __init__(self, detector: RecurrenceDetector | None = None, *, horizon_days: int = 90,
                 include_inferred_recurrence_on_request_date: bool = True) -> None:
        self.detector = detector or RecurrenceDetector()
        self.horizon_days = horizon_days
        self.include_inferred_recurrence_on_request_date = include_inferred_recurrence_on_request_date

    def build(self, ledger: CanonicalLedger, facts=()) -> ForecastTimeline:
        horizon = DateRange(ledger.as_of_date, ledger.as_of_date + timedelta(days=self.horizon_days))
        rules = self.detector.infer(ledger, facts)
        canonical = [
            ForecastOccurrence(flow, ForecastOrigin.CANONICAL_FUTURE)
            for flow in ledger.forecast_flows
            if horizon.contains(flow.effective_date)
        ]
        recurring: list[ForecastOccurrence] = []
        for rule in rules:
            dates = self._dates(rule, horizon)
            if not self.include_inferred_recurrence_on_request_date:
                dates = [item for item in dates if item > horizon.start]
            for index, occurrence_date in enumerate(dates):
                amount, evidence_ids = self._amended_amount(rule, occurrence_date, index, facts)
                if amount is None:
                    continue
                identity = json.dumps(
                    [ledger.request_id, rule.rule_id, occurrence_date.isoformat()], separators=(",", ":")
                )
                flow = CanonicalCashFlow(
                    flow_id="flow_recur_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                    user_id=ledger.user_id,
                    effective_date=occurrence_date,
                    amount=Money(amount, ledger.home_currency),
                    direction=rule.direction,
                    category=rule.category,
                    description=rule.description,
                    cash_state=(CashState.FUTURE_CONFIRMED if rule.direction is Direction.CREDIT
                                else CashState.FUTURE_RESERVED),
                    source_event_ids=rule.source_event_ids,
                    evidence_fact_ids=evidence_ids,
                    protected=rule.protected,
                    flexibility=rule.flexibility,
                    minimum_allowed_amount=rule.minimum_allowed_amount,
                )
                recurring.append(ForecastOccurrence(flow, ForecastOrigin.RECURRENCE, rule.rule_id))

        recurring = self._deduplicate_against_canonical(recurring, canonical)
        occurrences = tuple(sorted((*canonical, *recurring), key=lambda item: (
            item.flow.effective_date, item.flow.flow_id
        )))
        unresolved = tuple(sorted(
            item.event_id for item in ledger.event_dispositions if item.disposition.value == "unresolved"
        ))
        fingerprint = self._fingerprint(ledger, horizon, rules, occurrences, unresolved)
        return ForecastTimeline(
            ledger.request_id,
            ledger.user_id,
            ledger.home_currency,
            horizon,
            rules,
            occurrences,
            unresolved,
            fingerprint,
        )

    @staticmethod
    def _dates(rule: RecurrenceRule, horizon: DateRange) -> list[date]:
        dates: list[date] = []
        if rule.cadence is RecurrenceCadence.MONTHLY:
            step = 1
            while True:
                candidate = _add_months(
                    rule.anchor_date,
                    step * rule.interval,
                    day_of_month=rule.day_of_month,
                    end_of_month=rule.end_of_month,
                )
                if candidate > horizon.end:
                    break
                if candidate >= horizon.start and rule.active_range.contains(candidate):
                    dates.append(candidate)
                step += 1
        else:
            multiplier = {
                RecurrenceCadence.DAILY: 1,
                RecurrenceCadence.WEEKLY: 7,
                RecurrenceCadence.BIWEEKLY: 14,
                RecurrenceCadence.CUSTOM_DAYS: rule.interval,
            }[rule.cadence]
            if rule.cadence is not RecurrenceCadence.CUSTOM_DAYS:
                multiplier *= rule.interval
            candidate = rule.anchor_date + timedelta(days=multiplier)
            while candidate <= horizon.end:
                if candidate >= horizon.start and rule.active_range.contains(candidate):
                    dates.append(candidate)
                candidate += timedelta(days=multiplier)
        return dates

    @staticmethod
    def _matching_facts(rule, facts):
        source_ids = set(rule.source_event_ids)
        result = []
        for fact in facts:
            related = fact.related_event_id in source_ids
            subject = fact.subject.lower()
            semantic = rule.semantic_key.lower()
            category = rule.category.lower()
            topical = (
                (subject == "salary" and "salary" in semantic)
                or (subject == "rent" and ("rent" in category or "housing" in category))
                or (subject == "childcare" and "childcare" in category)
                or (subject == "service" and any(token in category for token in ("service", "subscription", "membership")))
            )
            if related or topical:
                result.append(fact)
        return result

    def _amended_amount(self, rule, occurrence_date, occurrence_index, facts):
        amount = rule.amount.amount
        used = set(rule.evidence_fact_ids)
        matching = self._matching_facts(rule, facts)
        cancellations = [fact for fact in matching if fact.fact_type is EvidenceFactType.CANCELLATION]
        if any(fact.effective_date is None or fact.effective_date <= occurrence_date for fact in cancellations):
            used.update(fact.fact_id for fact in cancellations)
            return None, tuple(sorted(used))
        amount_facts = [
            fact for fact in matching
            if fact.fact_type is EvidenceFactType.AMOUNT and isinstance(fact.value, Decimal)
            and fact.scope in {"recurring", "next_cycle"}
            and (fact.effective_date is None or fact.effective_date <= occurrence_date)
            and (fact.scope != "next_cycle" or occurrence_index == 0)
        ]
        recurring = [fact for fact in amount_facts if fact.scope != "next_cycle"]
        next_cycle = [fact for fact in amount_facts if fact.scope == "next_cycle"]
        if recurring:
            chosen = max(recurring, key=self._fact_order)
            amount = chosen.value
            used.add(chosen.fact_id)
        if occurrence_index == 0 and next_cycle:
            chosen = max(next_cycle, key=self._fact_order)
            amount = chosen.value
            used.add(chosen.fact_id)
        amendments = [
            fact for fact in matching
            if fact.fact_type is EvidenceFactType.AMENDMENT
            and isinstance(fact.value, str) and fact.value.startswith("increase_percent:")
            and (fact.effective_date is None or fact.effective_date <= occurrence_date)
        ]
        if amendments:
            chosen = max(amendments, key=self._fact_order)
            percent = Decimal(chosen.value.split(":", 1)[1])
            amount = amount * (Decimal("1") + percent / Decimal("100"))
            used.add(chosen.fact_id)
        return amount, tuple(sorted(used))

    @staticmethod
    def _fact_order(fact):
        return (
            fact.effective_date or date.min,
            fact.provenance.observed_at.isoformat() if fact.provenance.observed_at else "",
            fact.fact_id,
        )

    @staticmethod
    def _deduplicate_against_canonical(recurring, canonical):
        exact = {
            (item.flow.effective_date, item.flow.direction, item.flow.category.lower())
            for item in canonical
        }
        return [
            item for item in recurring
            if (item.flow.effective_date, item.flow.direction, item.flow.category.lower()) not in exact
        ]

    @staticmethod
    def _fingerprint(ledger, horizon, rules, occurrences, unresolved):
        payload = {
            "ledger": ledger.fingerprint,
            "horizon": [horizon.start.isoformat(), horizon.end.isoformat()],
            "rules": [[item.rule_id, item.cadence.value, item.interval, str(item.amount.amount),
                       item.amount_strategy.value, item.anchor_date.isoformat()] for item in rules],
            "occurrences": [[item.flow.flow_id, item.flow.effective_date.isoformat(),
                             item.flow.direction.value, str(item.flow.amount.amount), item.origin.value]
                            for item in occurrences],
            "unresolved": list(unresolved),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

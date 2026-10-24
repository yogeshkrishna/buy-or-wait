"""Deterministic request-as-of canonical financial ledger reconstruction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import hashlib
import json

from buywait.domain.enums import (
    CashState,
    Direction,
    EventStatus,
    EventType,
    EvidenceFactType,
    LifecycleDisposition,
)
from buywait.domain.errors import InvalidInputError, MissingExchangeRateError
from buywait.domain.fx import RateBook
from buywait.domain.models import (
    CanonicalCashFlow,
    CanonicalLedger,
    EvaluationBundle,
    EventDisposition,
    EventLifecycle,
    EvidenceFact,
    HistoricalSeriesCandidate,
    LedgerEvidenceDisposition,
    LedgerWarning,
    RawEvent,
)
from buywait.domain.money import Money
from buywait.evidence.targeting import fact_applies_to_event

from .lifecycle import LifecycleComponent, build_components


@dataclass(frozen=True, slots=True)
class _ResolvedEvent:
    event: RawEvent
    amount: Money | None
    settlement_date: date
    status: EventStatus
    outstanding_debit: bool
    evidence_fact_ids: tuple[str, ...]


_DISPOSITION_PRIORITY = {
    LifecycleDisposition.UNRESOLVED: 0,
    LifecycleDisposition.FUTURE_DEBIT: 1,
    LifecycleDisposition.FUTURE_CONFIRMED_CREDIT: 2,
    LifecycleDisposition.INTERNAL_TRANSFER: 3,
    LifecycleDisposition.NON_CASH: 4,
    LifecycleDisposition.PENDING_CREDIT_IGNORED: 5,
    LifecycleDisposition.FAILED_OR_CANCELLED: 6,
    LifecycleDisposition.DUPLICATE: 7,
    LifecycleDisposition.HISTORICAL_BALANCE: 8,
}


class CanonicalLedgerBuilder:
    """Build one immutable ledger without replaying settled history into its balance."""

    def build(self, bundle: EvaluationBundle, evidence) -> CanonicalLedger:
        request = bundle.request
        profile = bundle.profile
        if evidence.request_id != request.request_id:
            raise InvalidInputError("Evidence and evaluation bundle request IDs differ.")

        components, graph_warnings = build_components(bundle.events)
        component_by_event = {
            event_id: component for component in components for event_id in component.event_ids
        }
        facts_by_event: dict[str, list[EvidenceFact]] = defaultdict(list)
        events_by_id = {event.event_id: event for event in bundle.events}
        for fact in evidence.facts:
            event = events_by_id.get(fact.related_event_id)
            if event is not None and fact_applies_to_event(fact, event):
                facts_by_event[fact.related_event_id].append(fact)

        used_fact_ids: set[str] = set()
        warnings = list(graph_warnings)
        resolved = {
            event.event_id: self._resolve_event(event, facts_by_event.get(event.event_id, ()), used_fact_ids)
            for event in bundle.events
        }
        self._apply_unlinked_salary_overrides(bundle, evidence.facts, resolved, used_fact_ids)

        internal_event_ids, internal_fact_ids, internal_warnings = self._internal_transfers(
            bundle, evidence.facts, resolved
        )
        used_fact_ids.update(internal_fact_ids)
        warnings.extend(internal_warnings)

        invalid_event_ids = {
            event_id for component in components if component.invalid for event_id in component.event_ids
        }
        children: dict[str, list[_ResolvedEvent]] = defaultdict(list)
        for item in resolved.values():
            if item.event.linked_event_id in resolved:
                children[item.event.linked_event_id].append(item)
        superseded_event_ids = self._superseded_representations(resolved, children)

        rate_book = RateBook(bundle.exchange_rates)
        flows: list[CanonicalCashFlow] = []
        dispositions: list[EventDisposition] = []
        flow_by_event: dict[str, str] = {}
        for event in sorted(bundle.events, key=lambda item: item.event_id):
            item = resolved[event.event_id]
            component = component_by_event[event.event_id]
            disposition, rule_code, explanation, state, effective_date = self._classify(
                item,
                request.request_date,
                invalid=event.event_id in invalid_event_ids,
                internal=event.event_id in internal_event_ids,
                superseded=event.event_id in superseded_event_ids,
                children=children.get(event.event_id, ()),
            )
            flow = None
            if state is not None and item.amount is not None:
                try:
                    flow = self._event_flow(
                        item, state, effective_date, profile, rate_book
                    )
                except MissingExchangeRateError as error:
                    disposition = LifecycleDisposition.UNRESOLVED
                    rule_code = "missing_exact_fx_rate"
                    explanation = "The cash event is unresolved because its exact dated FX rate is unavailable."
                    warnings.append(LedgerWarning(
                        error.code, error.message, (event.event_id,)
                    ))
            if flow is not None:
                flows.append(flow)
                flow_by_event[event.event_id] = flow.flow_id
            dispositions.append(EventDisposition(
                event.event_id,
                component.lifecycle_id,
                disposition,
                rule_code,
                explanation,
                flow.flow_id if flow else None,
                item.evidence_fact_ids,
            ))

        synthetic_flows, synthetic_used, synthetic_warnings = self._evidence_income_flows(
            bundle, evidence.facts, resolved, rate_book
        )
        used_fact_ids.update(synthetic_used)
        warnings.extend(synthetic_warnings)
        flows.extend(synthetic_flows)

        for fact_id in used_fact_ids:
            matching_events = [item.event.event_id for item in resolved.values() if fact_id in item.evidence_fact_ids]
            if matching_events:
                flow_id = next((flow_by_event[event_id] for event_id in matching_events if event_id in flow_by_event), None)
                if flow_id is not None:
                    flow_by_event[fact_id] = flow_id
        for flow in synthetic_flows:
            for fact_id in flow.evidence_fact_ids:
                flow_by_event[fact_id] = flow.flow_id

        evidence_dispositions = self._evidence_dispositions(
            evidence.facts, used_fact_ids, flow_by_event
        )
        warnings.extend(self._evidence_warnings(evidence))
        warnings = self._deduplicate_warnings(warnings)

        disposition_by_id = {item.event_id: item for item in dispositions}
        lifecycles = self._lifecycles(components, disposition_by_id, request.user_id)
        flows.sort(key=lambda item: (item.effective_date, item.flow_id))
        dispositions.sort(key=lambda item: item.event_id)
        multi_event_ids = {
            event_id for component in components if len(component.event_ids) > 1
            for event_id in component.event_ids
        }
        historical_series = self._historical_series(bundle, flows, resolved, multi_event_ids)
        fingerprint = self._fingerprint(
            bundle, flows, dispositions, evidence_dispositions, lifecycles,
            historical_series, warnings
        )
        return CanonicalLedger(
            request.request_id,
            request.user_id,
            request.request_date,
            profile.home_currency,
            profile.current_available_balance,
            profile.minimum_balance_to_keep,
            tuple(flows),
            tuple(dispositions),
            tuple(evidence_dispositions),
            tuple(lifecycles),
            tuple(historical_series),
            tuple(warnings),
            fingerprint,
        )

    @staticmethod
    def _resolve_event(event: RawEvent, facts, used_fact_ids: set[str]) -> _ResolvedEvent:
        settlement = event.settlement_date or event.event_date
        amount = event.amount
        status = event.status
        outstanding = False
        used: set[str] = set()

        dated_facts = [fact for fact in facts if fact.fact_type in {
            EvidenceFactType.SETTLEMENT_DATE, EvidenceFactType.EFFECTIVE_DATE
        } and isinstance(fact.value, date)]
        explicit_dates = [fact for fact in dated_facts if fact.explicit]
        if explicit_dates:
            chosen = max(explicit_dates, key=CanonicalLedgerBuilder._fact_order)
            settlement = chosen.value
            used.add(chosen.fact_id)

        amount_facts = [fact for fact in facts if fact.fact_type is EvidenceFactType.AMOUNT
                        and isinstance(fact.value, Decimal)
                        and (fact.effective_date is None or fact.effective_date <= settlement)]
        applicable = [fact for fact in amount_facts if amount is None or fact.explicit]
        if applicable:
            latest_date = max(fact.effective_date or date.min for fact in applicable)
            latest = [fact for fact in applicable if (fact.effective_date or date.min) == latest_date]
            value = (max if event.direction is Direction.DEBIT else min)(fact.value for fact in latest)
            chosen = min((fact for fact in latest if fact.value == value), key=lambda fact: fact.fact_id)
            amount = Money(chosen.value, chosen.currency or event.currency)
            used.add(chosen.fact_id)

        state_facts = [fact for fact in facts if fact.fact_type in {
            EvidenceFactType.CANCELLATION, EvidenceFactType.SETTLEMENT,
            EvidenceFactType.STATUS, EvidenceFactType.CASH_STATE,
        }]
        for fact in sorted(state_facts, key=CanonicalLedgerBuilder._fact_order):
            value = str(fact.value)
            if fact.fact_type is EvidenceFactType.CANCELLATION or value in {"cancelled", "income_ended"}:
                status = EventStatus.CANCELLED
                used.add(fact.fact_id)
            elif fact.fact_type is EvidenceFactType.SETTLEMENT or value == "settled":
                status = EventStatus.SETTLED
                used.add(fact.fact_id)
            elif value == "pending_credit":
                status = EventStatus.PENDING
                used.add(fact.fact_id)
            elif value == "unrealized":
                status = EventStatus.UNREALIZED
                used.add(fact.fact_id)
            elif value == "outstanding_debit":
                outstanding = True
                used.add(fact.fact_id)
            elif value == "confirmed":
                used.add(fact.fact_id)
        used_fact_ids.update(used)
        return _ResolvedEvent(event, amount, settlement, status, outstanding, tuple(sorted(used)))

    @staticmethod
    def _fact_order(fact: EvidenceFact) -> tuple:
        return (
            1 if fact.explicit else 0,
            fact.provenance.observed_at.isoformat() if fact.provenance.observed_at else "",
            fact.effective_date or date.min,
            fact.fact_id,
        )

    @staticmethod
    def _apply_unlinked_salary_overrides(bundle, facts, resolved, used_fact_ids) -> None:
        salary = [fact for fact in facts if fact.related_event_id is None and fact.subject == "salary"]
        if not salary:
            return
        future = [item for item in resolved.values()
                  if item.event.event_type is EventType.INCOME and item.event.category == "salary"
                  and item.status is EventStatus.SCHEDULED]
        future.sort(key=lambda item: (item.settlement_date, item.event.event_id))
        cancelled = [fact for fact in salary if fact.fact_type is EvidenceFactType.CANCELLATION
                     and fact.value == "income_ended"]
        if cancelled:
            for item in future:
                active = [fact for fact in cancelled if fact.effective_date is None or fact.effective_date <= item.settlement_date]
                if active:
                    chosen = max(active, key=CanonicalLedgerBuilder._fact_order)
                    resolved[item.event.event_id] = replace(
                        item, status=EventStatus.CANCELLED,
                        evidence_fact_ids=tuple(sorted(set(item.evidence_fact_ids) | {chosen.fact_id}))
                    )
                    used_fact_ids.add(chosen.fact_id)
        if not future:
            return
        first = resolved[future[0].event.event_id]
        date_facts = [fact for fact in salary if fact.fact_type in {
            EvidenceFactType.SETTLEMENT_DATE, EvidenceFactType.EFFECTIVE_DATE
        } and isinstance(fact.value, date) and fact.value >= bundle.request.request_date]
        if date_facts:
            chosen = max(date_facts, key=CanonicalLedgerBuilder._fact_order)
            first = replace(first, settlement_date=chosen.value,
                            evidence_fact_ids=tuple(sorted(set(first.evidence_fact_ids) | {chosen.fact_id})))
            used_fact_ids.add(chosen.fact_id)
        amount_facts = [fact for fact in salary if fact.fact_type is EvidenceFactType.AMOUNT
                        and isinstance(fact.value, Decimal)
                        and (fact.effective_date is None or fact.effective_date <= first.settlement_date)]
        if amount_facts:
            chosen = max(amount_facts, key=CanonicalLedgerBuilder._fact_order)
            first = replace(first, amount=Money(chosen.value, chosen.currency or first.event.currency),
                            evidence_fact_ids=tuple(sorted(set(first.evidence_fact_ids) | {chosen.fact_id})))
            used_fact_ids.add(chosen.fact_id)
        resolved[first.event.event_id] = first

    @staticmethod
    def _internal_transfers(bundle, facts, resolved):
        transfer_facts = [fact for fact in facts if fact.fact_type is EvidenceFactType.INTERNAL_TRANSFER
                          and fact.value == "internal"]
        if not transfer_facts:
            return set(), set(), []
        groups: dict[tuple, dict[Direction, list[_ResolvedEvent]]] = defaultdict(lambda: defaultdict(list))
        for item in resolved.values():
            if item.amount is None or item.status is not EventStatus.SETTLED:
                continue
            key = (item.settlement_date, item.amount.currency, item.amount.amount)
            if item.event.direction in {Direction.DEBIT, Direction.CREDIT}:
                groups[key][item.event.direction].append(item)
        pairs = []
        for directions in groups.values():
            if len(directions[Direction.DEBIT]) == 1 and len(directions[Direction.CREDIT]) == 1:
                pairs.append((directions[Direction.DEBIT][0], directions[Direction.CREDIT][0]))
        matched: set[str] = set()
        used: set[str] = set()
        warnings: list[LedgerWarning] = []
        for fact in transfer_facts:
            candidates = pairs
            if fact.related_event_id:
                candidates = [pair for pair in pairs if fact.related_event_id in {pair[0].event.event_id, pair[1].event.event_id}]
            if len(candidates) == 1:
                matched.update(item.event.event_id for item in candidates[0])
                used.add(fact.fact_id)
            elif candidates:
                warnings.append(LedgerWarning(
                    "internal_transfer_pair_unresolved",
                    "Internal-transfer evidence did not identify exactly one matching debit and credit pair.",
                    (fact.fact_id,),
                ))
        return matched, used, warnings

    @staticmethod
    def _superseded_representations(resolved, children):
        """Select an older same-cash representation replaced by a linked settlement.

        A pending duplicate card charge is deliberately *not* selected: it is a
        second unresolved debit and the safer interpretation is to reserve it.
        Failed/cancelled attempts have their own explicit dispositions.
        """
        superseded: set[str] = set()
        for parent_id, linked_children in children.items():
            parent = resolved[parent_id]
            if parent.status in {EventStatus.CANCELLED, EventStatus.FAILED}:
                continue
            for child in linked_children:
                same_cash_identity = (
                    child.status is EventStatus.SETTLED
                    and child.event.direction is parent.event.direction
                    and child.event.event_type is parent.event.event_type
                    and child.event.category == parent.event.category
                    and child.amount is not None
                    and parent.amount is not None
                    and child.amount == parent.amount
                )
                if same_cash_identity:
                    superseded.add(parent_id)
                    break
        return superseded

    @staticmethod
    def _classify(item, as_of, *, invalid, internal, superseded, children):
        event = item.event
        if invalid:
            return (LifecycleDisposition.UNRESOLVED, "invalid_lifecycle_graph",
                    "The event is unresolved because its lifecycle graph is invalid.", None, item.settlement_date)
        if internal:
            return (LifecycleDisposition.INTERNAL_TRANSFER, "verified_internal_transfer",
                    "A matching own-account debit and credit were excluded as a net-neutral internal transfer.", None, item.settlement_date)
        if superseded:
            return (LifecycleDisposition.DUPLICATE, "superseded_by_linked_settlement",
                    "A newer linked settled row represents the same cash movement, so the older row was excluded.",
                    None, item.settlement_date)
        if event.direction is Direction.NON_CASH or event.event_type is EventType.INVESTMENT_VALUATION or item.status is EventStatus.UNREALIZED:
            return (LifecycleDisposition.NON_CASH, "unrealized_or_non_cash",
                    "The record is non-cash evidence and cannot fund or reduce the forecast balance.", None, item.settlement_date)
        if item.status is EventStatus.CANCELLED:
            return (LifecycleDisposition.FAILED_OR_CANCELLED, "cancelled_cash_movement",
                    "The cancelled cash movement was excluded.", None, item.settlement_date)
        if item.status is EventStatus.FAILED:
            active_retry = any(child.event.direction is Direction.DEBIT and child.status in {EventStatus.PENDING, EventStatus.SCHEDULED}
                               for child in children)
            if item.outstanding_debit and not active_retry:
                return (LifecycleDisposition.FUTURE_DEBIT, "failed_but_still_outstanding",
                        "Newer evidence says the failed debit remains owed, so it is reserved immediately.",
                        CashState.FUTURE_RESERVED, max(as_of, item.settlement_date))
            return (LifecycleDisposition.FAILED_OR_CANCELLED, "failed_attempt_excluded",
                    "The failed attempt was excluded; any linked retry is evaluated separately.", None, item.settlement_date)
        if item.status is EventStatus.PENDING and event.direction is Direction.CREDIT:
            return (LifecycleDisposition.PENDING_CREDIT_IGNORED, "pending_credit_not_cash",
                    "The pending credit was ignored until settlement is confirmed.", None, item.settlement_date)
        if item.amount is None:
            return (LifecycleDisposition.UNRESOLVED, "mandatory_amount_unresolved",
                    "The event amount is unresolved and was never substituted with zero.", None, item.settlement_date)
        if item.status in {EventStatus.PENDING, EventStatus.SCHEDULED}:
            if event.direction is Direction.DEBIT:
                return (LifecycleDisposition.FUTURE_DEBIT, "pending_or_scheduled_debit_reserved",
                        "The supplied pending or scheduled debit is reserved on its conservative supported date.",
                        CashState.FUTURE_RESERVED, max(as_of, item.settlement_date))
            if item.status is EventStatus.SCHEDULED and event.event_type is EventType.INCOME:
                return (LifecycleDisposition.FUTURE_CONFIRMED_CREDIT, "confirmed_scheduled_income",
                        "The supplied scheduled income is counted only on its supported settlement date.",
                        CashState.FUTURE_CONFIRMED, max(as_of, item.settlement_date))
            return (LifecycleDisposition.PENDING_CREDIT_IGNORED, "unsupported_future_credit",
                    "The future credit lacks the confirmation required to enter forecast cash.", None, item.settlement_date)
        if item.status is EventStatus.SETTLED:
            if item.settlement_date <= as_of:
                return (LifecycleDisposition.HISTORICAL_BALANCE, "settled_before_balance_anchor",
                        "The settled event remains historical evidence and is not replayed into current cash.",
                        CashState.HISTORICAL, item.settlement_date)
            if event.direction is Direction.DEBIT:
                return (LifecycleDisposition.FUTURE_DEBIT, "future_dated_settled_debit_reserved",
                        "A supplied future-dated debit is reserved conservatively.",
                        CashState.FUTURE_RESERVED, item.settlement_date)
            if event.event_type in {EventType.INCOME, EventType.INVESTMENT_SALE, EventType.REFUND}:
                return (LifecycleDisposition.FUTURE_CONFIRMED_CREDIT, "future_settlement_supported",
                        "The confirmed future settlement is counted on its supplied date.",
                        CashState.FUTURE_CONFIRMED, item.settlement_date)
        return (LifecycleDisposition.UNRESOLVED, "unsupported_event_state",
                "The supplied state cannot be mapped safely to cash without more evidence.", None, item.settlement_date)

    @staticmethod
    def _event_flow(item, state, effective_date, profile, rate_book):
        original = item.amount
        assert original is not None
        amount = original
        minimum = item.event.minimum_allowed_amount
        fx_rate = None
        fx_date = None
        if original.currency != profile.home_currency:
            fx_date = item.settlement_date
            rate = rate_book.lookup(fx_date, original.currency, profile.home_currency)
            fx_rate = rate.rate
            amount = rate_book.convert(original, profile.home_currency, fx_date)
            if minimum is not None:
                minimum = rate_book.convert(minimum, profile.home_currency, fx_date)
        identity = json.dumps([
            item.event.event_id, effective_date.isoformat(), str(amount.amount),
            amount.currency.value, state.value
        ], separators=(",", ":"))
        return CanonicalCashFlow(
            "flow_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            item.event.user_id,
            effective_date,
            amount,
            item.event.direction,
            item.event.category,
            item.event.description,
            state,
            (item.event.event_id,),
            item.evidence_fact_ids,
            item.event.category in profile.protected_categories,
            item.event.flexibility,
            minimum,
            original if original.currency != profile.home_currency else None,
            fx_rate,
            fx_date,
        )

    def _evidence_income_flows(self, bundle, facts, resolved, rate_book):
        detached = [
            fact for fact in facts
            if fact.related_event_id is None
            or (
                fact.related_event_id in resolved
                and not fact_applies_to_event(fact, resolved[fact.related_event_id].event)
            )
        ]
        salary_facts = [fact for fact in detached if fact.subject == "salary"]
        future_structured_salary = any(
            item.event.event_type is EventType.INCOME and item.event.category == "salary"
            and item.status is EventStatus.SCHEDULED for item in resolved.values()
        )
        flows: list[CanonicalCashFlow] = []
        used: set[str] = set()
        warnings: list[LedgerWarning] = []
        historical_salary = [item for item in resolved.values()
                             if item.event.event_type is EventType.INCOME and item.event.category == "salary"
                             and item.status is EventStatus.SETTLED and item.amount is not None
                             and item.settlement_date <= bundle.request.request_date]
        historical_salary.sort(key=lambda item: (item.settlement_date, item.event.event_id))
        confirmed = [fact for fact in salary_facts if fact.fact_type is EvidenceFactType.STATUS and fact.value == "confirmed"]
        cancelled = any(fact.fact_type is EvidenceFactType.CANCELLATION and fact.value == "income_ended" for fact in salary_facts)
        if confirmed and not cancelled and not future_structured_salary and historical_salary:
            date_candidates: list[tuple[date, EvidenceFact]] = []
            for fact in salary_facts:
                if isinstance(fact.value, date):
                    candidate = fact.value
                else:
                    candidate = fact.effective_date
                if candidate is not None and candidate >= bundle.request.request_date:
                    date_candidates.append((candidate, fact))
            if date_candidates:
                pay_date, date_fact = min(date_candidates, key=lambda item: (item[0], item[1].fact_id))
                amount_facts = [fact for fact in salary_facts if fact.fact_type is EvidenceFactType.AMOUNT
                                and isinstance(fact.value, Decimal)
                                and (fact.effective_date is None or fact.effective_date <= pay_date)]
                latest = historical_salary[-1]
                if amount_facts:
                    amount_fact = max(amount_facts, key=self._fact_order)
                    amount = Money(amount_fact.value, amount_fact.currency or bundle.profile.home_currency)
                else:
                    amount_fact = None
                    amount = latest.amount
                if amount is not None:
                    fact_ids = {max(confirmed, key=self._fact_order).fact_id, date_fact.fact_id}
                    if amount_fact:
                        fact_ids.add(amount_fact.fact_id)
                    try:
                        flow = self._synthetic_income_flow(
                            bundle, "salary", pay_date, amount, latest.event.event_id,
                            tuple(sorted(fact_ids)), rate_book
                        )
                        flows.append(flow)
                        used.update(fact_ids)
                    except MissingExchangeRateError:
                        warnings.append(LedgerWarning(
                            "evidence_income_fx_unsupported",
                            "Evidence-only income was deferred because no supplied rate supports its inferred future date.",
                            tuple(sorted(fact.fact_id for fact in salary_facts)),
                        ))
                else:
                    warnings.append(LedgerWarning(
                        "evidence_income_amount_unresolved",
                        "Evidence-only income was deferred because no supported amount could be reconstructed.",
                        tuple(sorted(fact.fact_id for fact in salary_facts)),
                    ))

        # A dated one-time employer adjustment is separate from recurring salary.
        confirmed_sources = {fact.provenance.source_identity or fact.provenance.source_id for fact in confirmed}
        for fact in detached:
            source = fact.provenance.source_identity or fact.provenance.source_id
            if (fact.subject != "arrears" or fact.fact_type is not EvidenceFactType.AMOUNT
                    or not isinstance(fact.value, Decimal) or fact.effective_date is None
                    or fact.effective_date < bundle.request.request_date or source not in confirmed_sources
                    or not historical_salary):
                continue
            currency = fact.currency or bundle.profile.home_currency
            try:
                flow = self._synthetic_income_flow(
                    bundle, "arrears", fact.effective_date, Money(fact.value, currency),
                    historical_salary[-1].event.event_id, (fact.fact_id,), rate_book
                )
                flows.append(flow)
                used.add(fact.fact_id)
            except MissingExchangeRateError:
                warnings.append(LedgerWarning(
                    "evidence_income_fx_unsupported",
                    "A one-time employer adjustment was deferred because its future FX rate is not supplied.",
                    (fact.fact_id,),
                ))
        return flows, used, warnings

    @staticmethod
    def _synthetic_income_flow(bundle, subject, effective_date, original_amount, source_event_id, fact_ids, rate_book):
        amount = original_amount
        fx_rate = None
        fx_rate_date = None
        if original_amount.currency != bundle.profile.home_currency:
            rate = rate_book.lookup(effective_date, original_amount.currency, bundle.profile.home_currency)
            amount = rate_book.convert(original_amount, bundle.profile.home_currency, effective_date)
            fx_rate = rate.rate
            fx_rate_date = effective_date
        identity = json.dumps([
            bundle.request.request_id, subject, effective_date.isoformat(), str(amount.amount), *fact_ids
        ], separators=(",", ":"))
        return CanonicalCashFlow(
            "flow_evidence_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
            bundle.request.user_id,
            effective_date,
            amount,
            Direction.CREDIT,
            "salary",
            "Confirmed employer income from dated evidence",
            CashState.FUTURE_CONFIRMED,
            (source_event_id,),
            fact_ids,
            protected=True,
            original_amount=original_amount if original_amount.currency != amount.currency else None,
            fx_rate=fx_rate,
            fx_rate_date=fx_rate_date,
        )

    @staticmethod
    def _evidence_dispositions(facts, used_fact_ids, flow_by_fact):
        result = []
        for fact in sorted(facts, key=lambda item: item.fact_id):
            if fact.fact_id in used_fact_ids:
                result.append(LedgerEvidenceDisposition(
                    fact.fact_id, "applied", "evidence_applied_to_ledger",
                    "The selected fact supported a cash amount, date, state, or exclusion.",
                    fact.related_event_id, flow_by_fact.get(fact.fact_id)
                ))
            elif fact.scope in {"recurring", "next_cycle"} or fact.subject in {"rent", "childcare", "service", "salary"}:
                result.append(LedgerEvidenceDisposition(
                    fact.fact_id, "deferred_to_recurrence", "recurrence_phase_owns_fact",
                    "The fact is preserved for recurrence inference and does not create cash by itself.",
                    fact.related_event_id
                ))
            else:
                result.append(LedgerEvidenceDisposition(
                    fact.fact_id, "excluded", "insufficient_cash_confirmation",
                    "The fact did not independently prove a cash movement and was excluded conservatively.",
                    fact.related_event_id
                ))
        return tuple(result)

    @staticmethod
    def _evidence_warnings(evidence):
        warnings = []
        for issue in evidence.issues:
            if issue.reason_code in {"image_amount_unrecognized", "message_template_unrecognized"} and not issue.mandatory:
                continue
            warnings.append(LedgerWarning(
                "evidence_" + issue.reason_code,
                "Evidence preprocessing reported a bounded unresolved or review condition.",
                tuple(value for value in (issue.source_id, issue.related_event_id) if value),
            ))
        for conflict in evidence.conflicts:
            if conflict.selected_fact_id is None:
                warnings.append(LedgerWarning(
                    "unresolved_evidence_conflict",
                    "Conflicting evidence had no safe precedence winner.",
                    conflict.fact_ids,
                ))
        return warnings

    @staticmethod
    def _deduplicate_warnings(warnings):
        unique = {(item.code, item.message, item.related_ids): item for item in warnings}
        return tuple(sorted(unique.values(), key=lambda item: (item.code, item.related_ids, item.message)))

    @staticmethod
    def _lifecycles(components, dispositions, user_id):
        result = []
        for component in components:
            rows = [dispositions[event_id] for event_id in component.event_ids]
            aggregate = min((row.disposition for row in rows), key=lambda item: _DISPOSITION_PRIORITY[item])
            candidates = [row for row in rows if row.flow_id is not None]
            canonical = min(candidates, key=lambda row: (_DISPOSITION_PRIORITY[row.disposition], row.event_id)).event_id if candidates else None
            result.append(EventLifecycle(
                component.lifecycle_id,
                user_id,
                component.event_ids,
                aggregate,
                canonical,
                tuple(sorted({fact_id for row in rows for fact_id in row.evidence_fact_ids})),
                tuple(sorted({row.rule_code for row in rows})),
            ))
        return tuple(result)

    @staticmethod
    def _historical_series(bundle, flows, resolved, multi_event_ids):
        by_event = {item.event.event_id: item.event for item in resolved.values()}
        grouped: dict[tuple, list[CanonicalCashFlow]] = defaultdict(list)
        for flow in flows:
            if flow.cash_state is not CashState.HISTORICAL:
                continue
            event = by_event.get(flow.source_event_ids[0])
            if event is None or event.event_type in {
                EventType.REFUND, EventType.INVESTMENT_PURCHASE,
                EventType.INVESTMENT_SALE, EventType.INVESTMENT_VALUATION,
            }:
                continue
            # Linked reversals, reimbursements, retries, replacements and disputes
            # are lifecycle evidence, not clean observations of recurring spend.
            if event.event_id in multi_event_ids:
                continue
            if any(token in event.description.lower() for token in ("reversed", "authorization", "reimbursable", "duplicate")):
                continue
            grouped[(flow.direction, flow.category)].append(flow)
        result = []
        for (direction, category), members in sorted(grouped.items(), key=lambda item: (item[0][0].value, item[0][1])):
            if len(members) < 2:
                continue
            flow_ids = tuple(flow.flow_id for flow in sorted(members, key=lambda flow: (flow.effective_date, flow.flow_id)))
            identity = bundle.request.user_id + "\0" + direction.value + "\0" + category + "\0" + "\0".join(flow_ids)
            result.append(HistoricalSeriesCandidate(
                "series_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                bundle.request.user_id,
                category,
                direction,
                flow_ids,
                tuple(sorted({fact_id for flow in members for fact_id in flow.evidence_fact_ids})),
            ))
        return tuple(result)

    @staticmethod
    def _fingerprint(bundle, flows, dispositions, evidence_dispositions, lifecycles, series, warnings):
        payload = {
            "request_id": bundle.request.request_id,
            "user_id": bundle.request.user_id,
            "as_of": bundle.request.request_date.isoformat(),
            "balance": str(bundle.profile.current_available_balance.amount),
            "currency": bundle.profile.home_currency.value,
            "flows": [[flow.flow_id, flow.effective_date.isoformat(), str(flow.amount.amount),
                       flow.amount.currency.value, flow.direction.value, flow.cash_state.value,
                       list(flow.source_event_ids), list(flow.evidence_fact_ids),
                       str(flow.original_amount.amount) if flow.original_amount else None,
                       flow.original_amount.currency.value if flow.original_amount else None,
                       str(flow.fx_rate) if flow.fx_rate is not None else None,
                       flow.fx_rate_date.isoformat() if flow.fx_rate_date else None]
                      for flow in flows],
            "events": [[item.event_id, item.lifecycle_id, item.disposition.value,
                        item.rule_code, item.flow_id, list(item.evidence_fact_ids)]
                       for item in dispositions],
            "evidence": [[item.fact_id, item.action, item.rule_code, item.related_event_id, item.flow_id]
                         for item in evidence_dispositions],
            "lifecycles": [[item.lifecycle_id, list(item.event_ids), item.disposition.value,
                            item.canonical_event_id, list(item.reason_codes)] for item in lifecycles],
            "series": [[item.series_id, item.direction.value, item.category, list(item.source_flow_ids)]
                       for item in series],
            "warnings": [[item.code, list(item.related_ids)] for item in warnings],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

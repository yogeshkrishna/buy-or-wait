"""Pure candidate construction, verification, spending-change search, and selection."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
import hashlib
from itertools import combinations, product
import json

from buywait.domain.enums import (
    CandidateRejectionCode,
    Direction,
    Flexibility,
    PaymentMethod,
    SpendingChangeAction,
    ViolationCode,
)
from buywait.domain.errors import BuyWaitError
from buywait.domain.models import (
    CandidatePlan,
    CapacityAssessment,
    EvaluationBundle,
    Payment,
    PaymentOption,
    SpendingChange,
)
from buywait.domain.money import Money
from buywait.forecasting import ForecastSimulator

from .ranking import RankKey, rank_key


@dataclass(frozen=True, slots=True)
class PlanSearchResult:
    request_id: str
    candidates: tuple[CandidatePlan, ...]
    winner: CandidatePlan | None
    winning_rank: RankKey | None
    fingerprint: str

    @property
    def rejection_counts(self) -> dict[str, int]:
        counts = Counter(code.value for item in self.candidates for code in item.rejection_codes)
        return dict(sorted(counts.items()))


class PlanOptimizer:
    """Enumerate legal choices and trust only ForecastSimulator for safety."""

    def __init__(self, simulator: ForecastSimulator | None = None) -> None:
        self.simulator = simulator or ForecastSimulator()

    def search(self, bundle: EvaluationBundle, assessment: CapacityAssessment) -> PlanSearchResult:
        bases = self._base_candidates(bundle, assessment)
        evaluated = tuple(self._verify(item, bundle, assessment) for item in bases)
        safe = [item for item in evaluated if self._selectable(item)]

        # The official ranking always prefers no-change plans. If any exists,
        # no changed candidate can win, so an exponential search is unnecessary.
        changed: list[CandidatePlan] = []
        if not safe and not assessment.timeline.unresolved_event_ids:
            change_sets = self._change_sets(bundle, assessment)
            for item in evaluated:
                if set(item.rejection_codes) != {CandidateRejectionCode.BELOW_FLOOR}:
                    continue
                if item.payment_method is PaymentMethod.WAIT or not item.payments:
                    continue
                for changes in change_sets:
                    variant = self._candidate(
                        bundle.request.request_id,
                        item.payment_method,
                        item.payments,
                        changes,
                        item.total_payable_amount,
                        item.payment_option_id,
                    )
                    changed.append(self._verify(variant, bundle, assessment))
            safe = [item for item in changed if self._selectable(item)]

        candidates = self._deduplicate((*evaluated, *changed))
        selectable = [item for item in candidates if self._selectable(item)]
        winner = min(selectable, key=lambda item: rank_key(item, bundle.request.desired_completion_date)) \
            if selectable else None
        winner_rank = rank_key(winner, bundle.request.desired_completion_date) if winner else None
        fingerprint = self._fingerprint(bundle.request.request_id, candidates, winner)
        return PlanSearchResult(bundle.request.request_id, candidates, winner, winner_rank, fingerprint)

    def _base_candidates(self, bundle, assessment):
        request = bundle.request
        currency = request.requested_amount.currency
        result = [self._candidate(
            request.request_id,
            PaymentMethod.FULL_PAYMENT,
            (Payment(request.request_date, request.requested_amount, 1),),
            (),
            request.requested_amount,
        )]

        partial_codes: tuple[CandidateRejectionCode, ...] = ()
        partial_payments: tuple[Payment, ...] = ()
        safe = assessment.amount_safe_to_pay.amount
        earliest = assessment.earliest_date_for_full_payment
        if not request.allows_partial_payment:
            partial_codes += (CandidateRejectionCode.PARTIAL_NOT_ALLOWED,)
        if safe <= 0 or safe >= request.requested_amount.amount or earliest is None:
            partial_codes += (CandidateRejectionCode.NO_SAFE_DATE,)
        else:
            partial_payments = (
                Payment(request.request_date, Money(safe, currency), 1),
                Payment(earliest, Money(request.requested_amount.amount - safe, currency), 2),
            )
        result.append(self._candidate(
            request.request_id,
            PaymentMethod.PARTIAL_PAYMENT,
            partial_payments,
            (),
            request.requested_amount,
            initial_codes=partial_codes,
        ))

        for option in bundle.payment_options:
            if option.payment_method is not PaymentMethod.INSTALLMENTS:
                continue
            result.append(self._candidate(
                request.request_id,
                PaymentMethod.INSTALLMENTS,
                self._option_payments(option),
                (),
                option.total_payable_amount,
                option.payment_option_id,
            ))

        wait_codes: tuple[CandidateRejectionCode, ...] = ()
        wait_payments: tuple[Payment, ...] = ()
        if earliest is None or earliest <= request.request_date:
            wait_codes = (CandidateRejectionCode.NO_SAFE_DATE,)
        else:
            wait_payments = (Payment(earliest, request.requested_amount, 1),)
        result.append(self._candidate(
            request.request_id,
            PaymentMethod.WAIT,
            wait_payments,
            (),
            request.requested_amount,
            initial_codes=wait_codes,
        ))
        return tuple(result)

    @staticmethod
    def _option_payments(option: PaymentOption) -> tuple[Payment, ...]:
        assert option.payment_frequency_days is not None
        return tuple(
            Payment(
                option.first_payment_date + timedelta(days=index * option.payment_frequency_days),
                option.payment_amount,
                index + 1,
            )
            for index in range(option.number_of_payments)
        )

    def _verify(self, candidate, bundle, assessment):
        request, profile = bundle.request, bundle.profile
        codes = set(candidate.rejection_codes)
        accepted_method = (
            PaymentMethod.FULL_PAYMENT if candidate.payment_method is PaymentMethod.WAIT
            else candidate.payment_method
        )
        if accepted_method not in profile.payment_methods_user_will_consider:
            codes.add(CandidateRejectionCode.METHOD_NOT_ACCEPTED)
        if candidate.payment_method is PaymentMethod.PARTIAL_PAYMENT and not request.allows_partial_payment:
            codes.add(CandidateRejectionCode.PARTIAL_NOT_ALLOWED)
        if not candidate.payments:
            codes.add(CandidateRejectionCode.NO_SAFE_DATE)
        if candidate.completion_date and candidate.completion_date > request.desired_completion_date:
            codes.add(CandidateRejectionCode.AFTER_DEADLINE)

        if candidate.payment_method is PaymentMethod.INSTALLMENTS:
            option = next((item for item in bundle.payment_options
                           if item.payment_option_id == candidate.payment_option_id), None)
            if option is None or option.payment_method is not PaymentMethod.INSTALLMENTS:
                codes.add(CandidateRejectionCode.OPTION_NOT_SUPPLIED)
            else:
                if profile.max_installment_months is None or (
                    option.number_of_payments > profile.max_installment_months
                ):
                    codes.add(CandidateRejectionCode.INSTALLMENT_TERM_EXCEEDED)
                if candidate.payments != self._option_payments(option):
                    codes.add(CandidateRejectionCode.OPTION_NOT_SUPPLIED)
                if candidate.total_payable_amount != option.total_payable_amount:
                    codes.add(CandidateRejectionCode.PAYMENT_SUM_MISMATCH)
        elif candidate.payment_method in {
            PaymentMethod.FULL_PAYMENT, PaymentMethod.PARTIAL_PAYMENT, PaymentMethod.WAIT
        }:
            if candidate.total_payable_amount != request.requested_amount:
                codes.add(CandidateRejectionCode.PAYMENT_SUM_MISMATCH)

        simulation = None
        structural = codes - {
            CandidateRejectionCode.BELOW_FLOOR,
            CandidateRejectionCode.UNRESOLVED_EVIDENCE,
        }
        if not structural and candidate.payments:
            try:
                simulation = self.simulator.simulate(
                    assessment.timeline,
                    profile.current_available_balance,
                    profile.minimum_balance_to_keep,
                    payments=candidate.payments,
                    spending_changes=candidate.spending_changes,
                )
                for violation in simulation.safety_result.violations:
                    if violation.code is ViolationCode.UNRESOLVED_EVIDENCE:
                        codes.add(CandidateRejectionCode.UNRESOLVED_EVIDENCE)
                    elif violation.code is ViolationCode.ILLEGAL_SPENDING_CHANGE:
                        codes.add(CandidateRejectionCode.ILLEGAL_CHANGE)
                    elif violation.code is ViolationCode.BELOW_MINIMUM_BALANCE:
                        codes.add(CandidateRejectionCode.BELOW_FLOOR)
                    else:
                        codes.add(CandidateRejectionCode.INVARIANT_FAILURE)
                if not simulation.safety_result.safe and not simulation.safety_result.violations:
                    codes.add(CandidateRejectionCode.BELOW_FLOOR)
            except (BuyWaitError, AssertionError, ValueError, TypeError):
                codes.add(CandidateRejectionCode.INVARIANT_FAILURE)
        return replace(
            candidate,
            eligible=not codes,
            safety_result=simulation.safety_result if simulation else None,
            rejection_codes=tuple(sorted(codes, key=lambda item: item.value)),
        )

    def _change_sets(self, bundle, assessment):
        events = {item.event_id: item for item in bundle.events}
        by_event: dict[str, tuple[SpendingChange, ...]] = {}
        for rule in assessment.timeline.recurrence_rules:
            if rule.direction is not Direction.DEBIT or rule.protected:
                continue
            sources = [events[item] for item in rule.source_event_ids if item in events]
            if not sources:
                continue
            latest = max(sources, key=lambda item: (item.settlement_date or item.event_date, item.event_id))
            actions: list[SpendingChange] = []
            category = rule.category.lower()
            if (category in bundle.profile.stoppable_categories
                    and rule.flexibility in {Flexibility.STOPPABLE, Flexibility.REDUCIBLE_OR_STOPPABLE}):
                actions.append(SpendingChange(SpendingChangeAction.STOP, latest.event_id))
            if (category in bundle.profile.reducible_categories
                    and rule.flexibility in {Flexibility.REDUCIBLE, Flexibility.REDUCIBLE_OR_STOPPABLE}
                    and rule.minimum_allowed_amount is not None
                    and rule.minimum_allowed_amount.amount < rule.amount.amount):
                actions.append(SpendingChange(
                    SpendingChangeAction.REDUCE_TO, latest.event_id, rule.minimum_allowed_amount
                ))
            if actions:
                by_event[latest.event_id] = tuple(sorted(actions, key=lambda item: item.action.value))
        event_ids = sorted(by_event)
        result = []
        for size in range(1, min(3, len(event_ids)) + 1):
            for selected in combinations(event_ids, size):
                for choices in product(*(by_event[event_id] for event_id in selected)):
                    result.append(tuple(sorted(choices, key=lambda item: item.event_id)))
        return tuple(result)

    @staticmethod
    def _candidate(request_id, method, payments, changes, total, option_id=None, initial_codes=()):
        payload = {
            "request": request_id,
            "method": method.value,
            "option": option_id,
            "payments": [[item.payment_date.isoformat(), str(item.amount.amount)] for item in payments],
            "changes": [[item.action.value, item.event_id,
                         str(item.new_amount.amount) if item.new_amount else None] for item in changes],
        }
        candidate_id = "candidate_" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        return CandidatePlan(
            candidate_id,
            request_id,
            method,
            tuple(payments),
            tuple(changes),
            total,
            option_id,
            not initial_codes,
            payments[-1].payment_date if payments else None,
            None,
            tuple(initial_codes),
        )

    @staticmethod
    def _selectable(candidate):
        return bool(candidate.eligible and candidate.safety_result and candidate.safety_result.safe)

    @staticmethod
    def _deduplicate(candidates):
        by_id = {item.candidate_id: item for item in candidates}
        return tuple(sorted(by_id.values(), key=lambda item: item.candidate_id))

    @staticmethod
    def _fingerprint(request_id, candidates, winner):
        payload = {
            "request": request_id,
            "candidates": [[item.candidate_id, item.eligible,
                            [code.value for code in item.rejection_codes],
                            item.safety_result.forecast_fingerprint if item.safety_result else None]
                           for item in candidates],
            "winner": winner.candidate_id if winner else None,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

"""Independent replay of a final decision against challenge invariants."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from buywait.domain.enums import (
    AffordabilityStatus,
    Direction,
    Flexibility,
    PaymentMethod,
    SpendingChangeAction,
)
from buywait.domain.models import Payment, SpendingChange
from buywait.forecasting import ForecastSimulator
from buywait.domain.money import floor_to_minor_unit


@dataclass(frozen=True, slots=True)
class DecisionValidation:
    valid: bool
    errors: tuple[str, ...]


class DecisionValidator:
    """Validate serialized meaning, legal options and safety without trusting search flags."""

    def __init__(self, simulator: ForecastSimulator | None = None) -> None:
        self.simulator = simulator or ForecastSimulator()

    def validate(self, decision, bundle, assessment) -> DecisionValidation:
        request, profile = bundle.request, bundle.profile
        errors: list[str] = []
        if decision.request_id != request.request_id:
            errors.append("request_id_mismatch")
        allowed_safe_amounts = {
            assessment.amount_safe_to_pay.amount,
            floor_to_minor_unit(assessment.amount_safe_to_pay.amount),
        }
        if (decision.amount_safe_to_pay.currency != assessment.amount_safe_to_pay.currency
                or decision.amount_safe_to_pay.amount not in allowed_safe_amounts):
            errors.append("safe_amount_mismatch")
        if decision.earliest_date_for_full_payment != assessment.earliest_date_for_full_payment:
            errors.append("earliest_date_mismatch")
        if any(payment.payment_date > request.desired_completion_date for payment in decision.payment_plan):
            errors.append("payment_after_deadline")

        method = decision.recommended_payment_method
        accepted = PaymentMethod.FULL_PAYMENT if method is PaymentMethod.WAIT else method
        if method is not PaymentMethod.NOT_RECOMMENDED and accepted not in profile.payment_methods_user_will_consider:
            errors.append("method_not_accepted")

        total = sum((item.amount.amount for item in decision.payment_plan), Decimal(0))
        if method not in {PaymentMethod.NOT_RECOMMENDED, PaymentMethod.INSTALLMENTS}:
            if total != request.requested_amount.amount:
                errors.append("payment_total_mismatch")
        if method is PaymentMethod.PARTIAL_PAYMENT:
            if not request.allows_partial_payment:
                errors.append("partial_not_allowed")
            expected = self._partial_payments(request, assessment)
            if decision.payment_plan != expected:
                errors.append("partial_schedule_mismatch")
        if method is PaymentMethod.INSTALLMENTS:
            matches = [option for option in bundle.payment_options
                       if option.payment_method is PaymentMethod.INSTALLMENTS
                       and self._option_payments(option) == decision.payment_plan]
            if len(matches) != 1:
                errors.append("installment_option_mismatch")
            else:
                option = matches[0]
                if total != option.total_payable_amount.amount:
                    errors.append("installment_total_mismatch")
                if (profile.max_installment_months is None
                        or option.number_of_payments > profile.max_installment_months):
                    errors.append("installment_term_exceeded")

        errors.extend(self._validate_changes(decision.spending_changes_needed, bundle, assessment))
        if method is PaymentMethod.NOT_RECOMMENDED:
            if decision.payment_plan or decision.spending_changes_needed:
                errors.append("not_recommended_has_plan")
        else:
            simulation = self.simulator.simulate(
                assessment.timeline,
                profile.current_available_balance,
                profile.minimum_balance_to_keep,
                payments=decision.payment_plan,
                spending_changes=decision.spending_changes_needed,
            )
            if not simulation.safety_result.safe:
                errors.append("winner_failed_safety_replay")

        expected_status = {
            PaymentMethod.PARTIAL_PAYMENT: AffordabilityStatus.AFFORDABLE_WITH_PLAN,
            PaymentMethod.INSTALLMENTS: AffordabilityStatus.AFFORDABLE_WITH_PLAN,
            PaymentMethod.WAIT: AffordabilityStatus.AFFORDABLE_LATER,
            PaymentMethod.NOT_RECOMMENDED: AffordabilityStatus.NOT_AFFORDABLE,
        }.get(method)
        if method is PaymentMethod.FULL_PAYMENT:
            expected_status = (AffordabilityStatus.AFFORDABLE_WITH_PLAN
                               if decision.spending_changes_needed
                               else AffordabilityStatus.AFFORDABLE_NOW)
        if decision.affordability_status is not expected_status:
            errors.append("status_method_mismatch")
        return DecisionValidation(not errors, tuple(sorted(set(errors))))

    def require_valid(self, decision, bundle, assessment) -> None:
        result = self.validate(decision, bundle, assessment)
        if not result.valid:
            raise ValueError(
                f"Decision {decision.request_id} failed independent validation: "
                + ", ".join(result.errors)
            )

    @staticmethod
    def _option_payments(option) -> tuple[Payment, ...]:
        from datetime import timedelta
        if option.payment_frequency_days is None:
            return (Payment(option.first_payment_date, option.payment_amount, 1),)
        return tuple(Payment(
            option.first_payment_date + timedelta(days=index * option.payment_frequency_days),
            option.payment_amount,
            index + 1,
        ) for index in range(option.number_of_payments))

    @staticmethod
    def _partial_payments(request, assessment) -> tuple[Payment, ...]:
        safe, earliest = assessment.amount_safe_to_pay, assessment.earliest_date_for_full_payment
        if earliest is None or not Decimal(0) < safe.amount < request.requested_amount.amount:
            return ()
        return (
            Payment(request.request_date, safe, 1),
            Payment(earliest, request.requested_amount - safe, 2),
        )

    @staticmethod
    def _validate_changes(changes: tuple[SpendingChange, ...], bundle, assessment) -> list[str]:
        errors: list[str] = []
        event_by_id = {item.event_id: item for item in bundle.events}
        rule_by_event = {
            event_id: rule
            for rule in assessment.timeline.recurrence_rules
            if rule.direction is Direction.DEBIT
            for event_id in rule.source_event_ids
        }
        for change in changes:
            event, rule = event_by_id.get(change.event_id), rule_by_event.get(change.event_id)
            if event is None or rule is None or rule.protected:
                errors.append("illegal_spending_change")
                continue
            category = rule.category.lower()
            if change.action is SpendingChangeAction.STOP:
                if (category not in bundle.profile.stoppable_categories
                        or rule.flexibility not in {Flexibility.STOPPABLE, Flexibility.REDUCIBLE_OR_STOPPABLE}):
                    errors.append("illegal_stop")
            else:
                minimum = rule.minimum_allowed_amount
                if (category not in bundle.profile.reducible_categories
                        or rule.flexibility not in {Flexibility.REDUCIBLE, Flexibility.REDUCIBLE_OR_STOPPABLE}
                        or change.new_amount is None or minimum is None
                        or change.new_amount.amount < minimum.amount
                        or change.new_amount.amount > rule.amount.amount):
                    errors.append("illegal_reduction")
        return errors

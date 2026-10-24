"""Capacity calculations over the deterministic forecast timeline."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from buywait.domain.errors import InvalidInputError
from buywait.domain.models import CapacityAssessment, CanonicalLedger, EvidenceFact, Payment
from buywait.domain.money import Money

from .simulation import ForecastSimulator
from .timeline import ForecastTimelineBuilder
from .recurrence import RecurrenceDetector


class ForecastEngine:
    def __init__(self, timeline_builder=None, simulator=None) -> None:
        self.simulator = simulator or ForecastSimulator()
        policy = self.simulator.policy
        self.timeline_builder = timeline_builder or ForecastTimelineBuilder(
            RecurrenceDetector(
                minimum_observations=policy.minimum_recurrence_observations,
                recent_window=policy.variable_essential_window,
                variable_essential_estimator=policy.variable_essential_estimator,
                other_recurring_debit_estimator=policy.other_recurring_debit_estimator,
            ),
            horizon_days=policy.horizon_days,
            include_inferred_recurrence_on_request_date=policy.include_inferred_recurrence_on_request_date,
        )

    def assess(self, ledger: CanonicalLedger, requested_amount: Money, facts=()) -> CapacityAssessment:
        if requested_amount.currency != ledger.home_currency:
            raise InvalidInputError("Requested amount must use the ledger currency.")
        timeline = self.timeline_builder.build(ledger, facts)
        baseline = self.simulator.simulate(
            timeline, ledger.current_available_balance, ledger.minimum_balance_to_keep
        )
        if baseline.safety_result.safe:
            balances = [day.lowest_intraday_balance.amount for day in baseline.days]
            if self.simulator.policy.confirmed_credits_before_same_day_payments:
                # Today's opening occurs BEFORE the proposed payment and does
                # not fall when it is made. The first affected minimum is the
                # closing balance (all day-zero credits precede all debits).
                # The unchanged opening floor is still checked by baseline.
                balances[0] = baseline.days[0].closing_balance.amount
            headroom = min(balances) - ledger.minimum_balance_to_keep.amount
            safe_amount = max(Decimal(0), min(requested_amount.amount, headroom))
        else:
            safe_amount = Decimal(0)
        earliest = self._earliest(
            timeline, ledger.current_available_balance, ledger.minimum_balance_to_keep, requested_amount
        )
        return CapacityAssessment(
            ledger.request_id,
            requested_amount,
            Money(safe_amount, ledger.home_currency),
            earliest,
            timeline,
            baseline,
        )

    def _earliest(self, timeline, starting, floor, requested):
        if timeline.unresolved_event_ids:
            return None
        if requested.amount == 0:
            baseline = self.simulator.simulate(timeline, starting, floor)
            return timeline.horizon.start if baseline.safety_result.safe else None
        candidate = timeline.horizon.start
        while candidate <= timeline.horizon.end:
            payment = Payment(candidate, requested, 1)
            result = self.simulator.simulate(timeline, starting, floor, payments=(payment,))
            if result.safety_result.safe:
                return candidate
            candidate += timedelta(days=1)
        return None

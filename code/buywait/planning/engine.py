"""Map verified plan search into the exact challenge Decision contract."""

from __future__ import annotations

import hashlib

from buywait.domain.enums import AffordabilityStatus, PaymentMethod
from buywait.domain.models import Decision

from .explanations import explain
from .search import PlanOptimizer, PlanSearchResult


class DecisionPlanner:
    def __init__(self, optimizer: PlanOptimizer | None = None) -> None:
        self.optimizer = optimizer or PlanOptimizer()

    def decide(self, bundle, assessment) -> tuple[Decision, PlanSearchResult]:
        search = self.optimizer.search(bundle, assessment)
        winner = search.winner
        if winner is None:
            status = AffordabilityStatus.NOT_AFFORDABLE
            method = PaymentMethod.NOT_RECOMMENDED
            payments = ()
            changes = ()
            selected = None
        else:
            method = winner.payment_method
            payments = winner.payments
            changes = winner.spending_changes
            selected = winner.candidate_id
            if method is PaymentMethod.FULL_PAYMENT:
                status = (AffordabilityStatus.AFFORDABLE_NOW if not changes
                          else AffordabilityStatus.AFFORDABLE_WITH_PLAN)
            elif method in {PaymentMethod.PARTIAL_PAYMENT, PaymentMethod.INSTALLMENTS}:
                status = AffordabilityStatus.AFFORDABLE_WITH_PLAN
            else:
                status = AffordabilityStatus.AFFORDABLE_LATER
        audit_id = "trace_" + hashlib.sha256(
            (assessment.timeline.fingerprint + search.fingerprint).encode()
        ).hexdigest()[:20]
        decision = Decision(
            request_id=bundle.request.request_id,
            request_date=bundle.request.request_date,
            requested_amount=bundle.request.requested_amount,
            amount_safe_to_pay=assessment.amount_safe_to_pay,
            affordability_status=status,
            recommended_payment_method=method,
            payment_plan=payments,
            earliest_date_for_full_payment=assessment.earliest_date_for_full_payment,
            spending_changes_needed=changes,
            decision_explanation=explain(bundle, assessment, winner),
            selected_candidate_id=selected,
            audit_trace_id=audit_id,
        )
        return decision, search


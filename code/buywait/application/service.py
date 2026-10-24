"""One shared request-to-decision service for CLI and evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from buywait.evidence.service import EvidenceService
from buywait.forecasting import ForecastEngine
from buywait.forecasting.policy import ForecastPolicy, RELEASE_CANDIDATE_POLICY
from buywait.forecasting.recurrence import RecurrenceDetector
from buywait.forecasting.simulation import ForecastSimulator
from buywait.forecasting.timeline import ForecastTimelineBuilder
from buywait.ledger import CanonicalLedgerBuilder
from buywait.planning import DecisionPlanner, PlanOptimizer
from buywait.planning.search import PlanSearchResult
from buywait.planning.validation import DecisionValidator


@dataclass(frozen=True, slots=True)
class DecisionResult:
    evidence: object
    ledger: object
    assessment: object
    search: PlanSearchResult
    decision: object


class BuyWaitDecisionService:
    def __init__(self, context, *, evidence_service=None, ledger_builder=None,
                 forecast_engine=None, planner=None, validator=None,
                 forecast_policy: ForecastPolicy = RELEASE_CANDIDATE_POLICY) -> None:
        self.context = context
        self.evidence_service = evidence_service or EvidenceService(context)
        self.ledger_builder = ledger_builder or CanonicalLedgerBuilder()
        simulator = ForecastSimulator(forecast_policy)
        detector = RecurrenceDetector(
            minimum_observations=forecast_policy.minimum_recurrence_observations,
            recent_window=forecast_policy.variable_essential_window,
            variable_essential_estimator=forecast_policy.variable_essential_estimator,
            other_recurring_debit_estimator=forecast_policy.other_recurring_debit_estimator,
        )
        timeline_builder = ForecastTimelineBuilder(
            detector,
            horizon_days=forecast_policy.horizon_days,
            include_inferred_recurrence_on_request_date=(
                forecast_policy.include_inferred_recurrence_on_request_date
            ),
        )
        self.forecast_engine = forecast_engine or ForecastEngine(timeline_builder, simulator)
        self.planner = planner or DecisionPlanner(PlanOptimizer(simulator))
        self.validator = validator or DecisionValidator(simulator=simulator)

    def decide(self, request_id: str) -> DecisionResult:
        bundle = self.context.bundle(request_id)
        evidence = self.evidence_service.evaluate(request_id)
        ledger = self.ledger_builder.build(bundle, evidence)
        assessment = self.forecast_engine.assess(ledger, bundle.request.requested_amount, evidence.facts)
        decision, search = self.planner.decide(bundle, assessment)
        self.validator.require_valid(decision, bundle, assessment)
        return DecisionResult(evidence, ledger, assessment, search, decision)

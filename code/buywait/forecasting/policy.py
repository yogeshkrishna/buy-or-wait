"""Versioned global forecast policy; never contains request-specific rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True, slots=True)
class ForecastPolicy:
    schema_version: str = "buywait.forecast-policy.v1"
    confirmed_credits_before_same_day_payments: bool = False
    include_inferred_recurrence_on_request_date: bool = True
    horizon_days: int = 90
    minimum_recurrence_observations: int = 3
    variable_essential_estimator: str = "nearest_rank_p75"
    variable_essential_window: int = 6
    other_recurring_debit_estimator: str = "latest"
    stable_payroll_strategy: str = "dominant_then_latest"
    missing_cycle_tolerance: str = "confirmed_payroll_only"
    evidence_amendment_scope: str = "effective_date_and_subject"
    safe_amount_rounding: str = "floor_minor_unit_at_csv_boundary"

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


PHASE07_BASELINE_POLICY = ForecastPolicy()
RELEASE_CANDIDATE_POLICY = ForecastPolicy(
    confirmed_credits_before_same_day_payments=True,
    variable_essential_estimator="nearest_rank_p50",
    variable_essential_window=8,
)

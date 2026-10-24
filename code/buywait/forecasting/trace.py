"""Privacy-safe request-scoped forecast diagnostics."""

from __future__ import annotations

from collections import Counter


def redacted_forecast_trace(assessment) -> dict:
    timeline = assessment.timeline
    baseline = assessment.baseline
    cadence = Counter(rule.cadence.value for rule in timeline.recurrence_rules)
    strategy = Counter(rule.amount_strategy.value for rule in timeline.recurrence_rules)
    origins = Counter(item.origin.value for item in timeline.occurrences)
    return {
        "schema_version": "buywait.forecast-trace.v1",
        "request_id": assessment.request_id,
        "horizon": {
            "start": timeline.horizon.start.isoformat(),
            "end": timeline.horizon.end.isoformat(),
            "inclusive_days": len(baseline.days),
        },
        "recurrence": {
            "rule_count": len(timeline.recurrence_rules),
            "cadences": dict(sorted(cadence.items())),
            "amount_strategies": dict(sorted(strategy.items())),
            "rules": [{
                "rule_id": rule.rule_id,
                "semantic_key": rule.semantic_key,
                "cadence": rule.cadence.value,
                "interval": rule.interval,
                "observed_count": rule.observed_count,
                "source_event_count": len(rule.source_event_ids),
                "source_event_ids_preview": list(rule.source_event_ids[:3]),
                "evidence_fact_ids": list(rule.evidence_fact_ids),
                "amount": "[REDACTED]",
            } for rule in timeline.recurrence_rules],
        },
        "timeline": {
            "occurrence_count": len(timeline.occurrences),
            "origins": dict(sorted(origins.items())),
            "unresolved_event_ids": list(timeline.unresolved_event_ids),
            "fingerprint": timeline.fingerprint,
        },
        "capacity": {
            "amount_safe_to_pay": "[REDACTED]",
            "earliest_date_for_full_payment": (
                assessment.earliest_date_for_full_payment.isoformat()
                if assessment.earliest_date_for_full_payment else None
            ),
            "baseline_safe": baseline.safety_result.safe,
            "tightest_date": (
                baseline.safety_result.tightest_date.isoformat()
                if baseline.safety_result.tightest_date else None
            ),
            "minimum_projected_balance": "[REDACTED]",
            "violation_codes": sorted({item.code.value for item in baseline.safety_result.violations}),
            "simulation_fingerprint": baseline.fingerprint,
        },
    }

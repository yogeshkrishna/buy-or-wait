"""Amount-redacted candidate and winner diagnostics."""

from __future__ import annotations

from collections import Counter


def redacted_decision_trace(result) -> dict:
    search, decision = result.search, result.decision
    methods = Counter(item.payment_method.value for item in search.candidates)
    eligible = Counter(item.payment_method.value for item in search.candidates if item.eligible)
    rejected = [item for item in search.candidates if item.rejection_codes]
    winner = search.winner
    return {
        "schema_version": "buywait.decision-trace.v1",
        "request_id": decision.request_id,
        "decision": {
            "status": decision.affordability_status.value,
            "method": decision.recommended_payment_method.value,
            "payment_count": len(decision.payment_plan),
            "change_event_ids": [item.event_id for item in decision.spending_changes_needed],
            "amount_safe_to_pay": "[REDACTED]",
            "earliest_date_for_full_payment": (
                decision.earliest_date_for_full_payment.isoformat()
                if decision.earliest_date_for_full_payment else None
            ),
            "audit_trace_id": decision.audit_trace_id,
        },
        "candidates": {
            "count": len(search.candidates),
            "by_method": dict(sorted(methods.items())),
            "eligible_by_method": dict(sorted(eligible.items())),
            "rejection_counts": search.rejection_counts,
            "rejected_preview": [{
                "candidate_id": item.candidate_id,
                "method": item.payment_method.value,
                "payment_option_id": item.payment_option_id,
                "codes": [code.value for code in item.rejection_codes],
            } for item in rejected[:5]],
        },
        "winner": None if winner is None else {
            "candidate_id": winner.candidate_id,
            "payment_option_id": winner.payment_option_id,
            "rank_key": [str(item) for item in search.winning_rank.as_tuple()],
            "safety_fingerprint": winner.safety_result.forecast_fingerprint,
            "minimum_projected_balance": "[REDACTED]",
        },
        "search_fingerprint": search.fingerprint,
    }


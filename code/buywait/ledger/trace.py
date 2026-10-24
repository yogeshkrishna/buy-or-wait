"""Bounded, amount-free ledger diagnostics for CLI and evaluation."""

from collections import Counter

from buywait.domain.enums import CashState, LifecycleDisposition


def redacted_ledger_trace(ledger) -> dict:
    actionable = [item for item in ledger.event_dispositions
                  if item.disposition is not LifecycleDisposition.HISTORICAL_BALANCE]
    pending_credit_ids = {
        item.event_id for item in ledger.event_dispositions
        if item.disposition is LifecycleDisposition.PENDING_CREDIT_IGNORED
    }
    pending_credits_in_forecast = sum(
        bool(pending_credit_ids.intersection(flow.source_event_ids))
        for flow in ledger.forecast_flows
    )
    return {
        "schema_version": "buywait.ledger-trace.v1",
        "request_id": ledger.request_id,
        "user_id": ledger.user_id,
        "as_of_date": ledger.as_of_date.isoformat(),
        "home_currency": ledger.home_currency.value,
        "balance_anchor": "profile_current_available_balance_not_replayed",
        "fingerprint": ledger.fingerprint,
        "counts": {
            "events": len(ledger.event_dispositions),
            "flows": len(ledger.flows),
            "historical_flows": len(ledger.historical_flows),
            "forecast_flows": len(ledger.forecast_flows),
            "lifecycles": len(ledger.lifecycles),
            "multi_event_lifecycles": sum(len(item.event_ids) > 1 for item in ledger.lifecycles),
            "series_candidates": len(ledger.historical_series_candidates),
            "warnings": len(ledger.warnings),
        },
        "disposition_counts": dict(sorted(Counter(item.disposition.value for item in ledger.event_dispositions).items())),
        "forecast_flow_counts": dict(sorted(Counter(
            ("credit" if flow.direction.value == "credit" else "debit") + ":" + flow.cash_state.value
            for flow in ledger.forecast_flows
        ).items())),
        "evidence_action_counts": dict(sorted(Counter(item.action for item in ledger.evidence_dispositions).items())),
        "actionable_dispositions": [
            {"event_id": item.event_id, "disposition": item.disposition.value,
             "rule_code": item.rule_code, "explanation": item.explanation}
            for item in actionable
        ],
        "multi_event_lifecycles": [
            {"lifecycle_id": item.lifecycle_id, "event_ids": list(item.event_ids),
             "disposition": item.disposition.value, "reason_codes": list(item.reason_codes)}
            for item in ledger.lifecycles if len(item.event_ids) > 1
        ],
        "warnings": [
            {"code": warning.code, "message": warning.message, "related_ids": list(warning.related_ids)}
            for warning in ledger.warnings
        ],
        "invariants": {
            "one_disposition_per_event": len({item.event_id for item in ledger.event_dispositions}) == len(ledger.event_dispositions),
            "pending_credits_in_forecast": pending_credits_in_forecast,
            "historical_flows_mutate_anchor": False,
            "all_forecast_flows_are_nonhistorical": all(flow.cash_state is not CashState.HISTORICAL for flow in ledger.forecast_flows),
        },
    }

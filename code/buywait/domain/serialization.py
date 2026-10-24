"""Deterministic wire and submission serialization for final domain decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from .enums import AffordabilityStatus, Currency, PaymentMethod, SpendingChangeAction
from .errors import InvalidInputError
from .models import Decision, Payment, SpendingChange
from .money import Money, exact_decimal_text, format_plan_amount, format_safe_amount


DECISION_SCHEMA_VERSION = "buywait.decision.v1"
OUTPUT_COLUMNS = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


def _money_to_dict(value: Money) -> dict[str, str]:
    return {"amount": exact_decimal_text(value.amount), "currency": value.currency.value}


def _money_from_dict(value: Mapping[str, Any]) -> Money:
    try:
        return Money.parse(str(value["amount"]), Currency(str(value["currency"])))
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidInputError("Invalid serialized money value.") from error


def decision_to_dict(decision: Decision) -> dict[str, Any]:
    """Return JSON-safe data with Decimal represented only as exact strings."""
    return {
        "schema_version": decision.schema_version,
        "request_id": decision.request_id,
        "request_date": decision.request_date.isoformat(),
        "requested_amount": _money_to_dict(decision.requested_amount),
        "amount_safe_to_pay": _money_to_dict(decision.amount_safe_to_pay),
        "affordability_status": decision.affordability_status.value,
        "recommended_payment_method": decision.recommended_payment_method.value,
        "payment_plan": [
            {
                "payment_date": payment.payment_date.isoformat(),
                "amount": _money_to_dict(payment.amount),
                "sequence": payment.sequence,
            }
            for payment in decision.payment_plan
        ],
        "earliest_date_for_full_payment": (
            decision.earliest_date_for_full_payment.isoformat()
            if decision.earliest_date_for_full_payment
            else None
        ),
        "spending_changes_needed": [
            {
                "action": change.action.value,
                "event_id": change.event_id,
                "new_amount": _money_to_dict(change.new_amount) if change.new_amount else None,
            }
            for change in decision.spending_changes_needed
        ],
        "decision_explanation": decision.decision_explanation,
        "selected_candidate_id": decision.selected_candidate_id,
        "audit_trace_id": decision.audit_trace_id,
    }


def decision_from_dict(payload: Mapping[str, Any]) -> Decision:
    if payload.get("schema_version") != DECISION_SCHEMA_VERSION:
        raise InvalidInputError(
            "Unsupported decision schema version.",
            context={"schema_version": str(payload.get("schema_version"))},
        )
    try:
        payments = tuple(
            Payment(
                payment_date=date.fromisoformat(str(item["payment_date"])),
                amount=_money_from_dict(item["amount"]),
                sequence=int(item["sequence"]),
            )
            for item in payload["payment_plan"]
        )
        changes = tuple(
            SpendingChange(
                action=SpendingChangeAction(str(item["action"])),
                event_id=str(item["event_id"]),
                new_amount=(
                    _money_from_dict(item["new_amount"])
                    if item.get("new_amount") is not None
                    else None
                ),
            )
            for item in payload["spending_changes_needed"]
        )
        earliest_value = payload.get("earliest_date_for_full_payment")
        return Decision(
            request_id=str(payload["request_id"]),
            request_date=date.fromisoformat(str(payload["request_date"])),
            requested_amount=_money_from_dict(payload["requested_amount"]),
            amount_safe_to_pay=_money_from_dict(payload["amount_safe_to_pay"]),
            affordability_status=AffordabilityStatus(str(payload["affordability_status"])),
            recommended_payment_method=PaymentMethod(str(payload["recommended_payment_method"])),
            payment_plan=payments,
            earliest_date_for_full_payment=(
                date.fromisoformat(str(earliest_value)) if earliest_value else None
            ),
            spending_changes_needed=changes,
            decision_explanation=str(payload["decision_explanation"]),
            selected_candidate_id=(
                str(payload["selected_candidate_id"])
                if payload.get("selected_candidate_id") is not None
                else None
            ),
            audit_trace_id=(
                str(payload["audit_trace_id"])
                if payload.get("audit_trace_id") is not None
                else None
            ),
            schema_version=str(payload["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidInputError("Invalid serialized decision.") from error


def decision_to_json(decision: Decision) -> str:
    return json.dumps(
        decision_to_dict(decision),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decision_from_json(payload: str) -> Decision:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise InvalidInputError("Decision JSON is invalid.") from error
    if not isinstance(parsed, dict):
        raise InvalidInputError("Decision JSON root must be an object.")
    return decision_from_dict(parsed)


def format_payment_plan(payments: tuple[Payment, ...]) -> str:
    if not payments:
        return "none"
    return "|".join(
        f"{payment.payment_date.isoformat()}:{format_plan_amount(payment.amount.amount)}"
        for payment in payments
    )


def format_spending_changes(changes: tuple[SpendingChange, ...]) -> str:
    if not changes:
        return "none"
    values: list[str] = []
    for change in changes:
        if change.action is SpendingChangeAction.STOP:
            values.append(f"stop:{change.event_id}")
        else:
            if change.new_amount is None:
                raise InvalidInputError("Reduce action is missing its amount.")
            values.append(
                f"reduce_to:{change.event_id}:{format_plan_amount(change.new_amount.amount)}"
            )
    return "|".join(values)


def decision_to_csv_row(decision: Decision) -> dict[str, str]:
    row = {
        "request_id": decision.request_id,
        "amount_safe_to_pay": format_safe_amount(decision.amount_safe_to_pay.amount),
        "affordability_status": decision.affordability_status.value,
        "recommended_payment_method": decision.recommended_payment_method.value,
        "payment_plan": format_payment_plan(decision.payment_plan),
        "earliest_date_for_full_payment": (
            decision.earliest_date_for_full_payment.isoformat()
            if decision.earliest_date_for_full_payment
            else ""
        ),
        "spending_changes_needed": format_spending_changes(decision.spending_changes_needed),
        "decision_explanation": decision.decision_explanation,
    }
    if tuple(row) != OUTPUT_COLUMNS:
        raise RuntimeError("Submission column order changed unexpectedly.")
    return row

"""Strict challenge CSV parsing, validation, and atomic export."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import tempfile

from buywait.domain.enums import AffordabilityStatus, PaymentMethod, SpendingChangeAction
from buywait.domain.errors import InvalidInputError
from buywait.domain.models import Decision, Payment, SpendingChange
from buywait.domain.money import Money
from buywait.domain.serialization import OUTPUT_COLUMNS, decision_to_csv_row


@dataclass(frozen=True, slots=True)
class OutputValidation:
    valid: bool
    row_count: int
    errors: tuple[str, ...]


def decision_from_csv_row(row: dict[str, str], request) -> Decision:
    currency = request.requested_amount.currency
    try:
        payments = _parse_payments(row["payment_plan"], currency)
        changes = _parse_changes(row["spending_changes_needed"], currency)
        earliest_text = row["earliest_date_for_full_payment"].strip()
        return Decision(
            request_id=row["request_id"].strip(),
            request_date=request.request_date,
            requested_amount=request.requested_amount,
            amount_safe_to_pay=Money.parse(row["amount_safe_to_pay"], currency),
            affordability_status=AffordabilityStatus(row["affordability_status"].strip()),
            recommended_payment_method=PaymentMethod(row["recommended_payment_method"].strip()),
            payment_plan=payments,
            earliest_date_for_full_payment=date.fromisoformat(earliest_text) if earliest_text else None,
            spending_changes_needed=changes,
            decision_explanation=row["decision_explanation"].strip(),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise InvalidInputError(
            "Invalid output row.", context={"request_id": row.get("request_id", "")}
        ) from error


def _parse_payments(value: str, currency) -> tuple[Payment, ...]:
    if value.strip() == "none":
        return ()
    result = []
    for sequence, item in enumerate(value.split("|"), 1):
        when, amount = item.split(":", 1)
        result.append(Payment(date.fromisoformat(when), Money.parse(amount, currency), sequence))
    return tuple(result)


def _parse_changes(value: str, currency) -> tuple[SpendingChange, ...]:
    if value.strip() == "none":
        return ()
    result = []
    for item in value.split("|"):
        parts = item.split(":")
        if len(parts) == 2 and parts[0] == "stop":
            result.append(SpendingChange(SpendingChangeAction.STOP, parts[1]))
        elif len(parts) == 3 and parts[0] == "reduce_to":
            result.append(SpendingChange(
                SpendingChangeAction.REDUCE_TO, parts[1], Money.parse(parts[2], currency)
            ))
        else:
            raise InvalidInputError("Invalid spending-change grammar.")
    return tuple(result)


def read_output(path: Path, context) -> tuple[Decision, ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != OUTPUT_COLUMNS:
            raise InvalidInputError("Output CSV columns or order do not match the challenge schema.")
        rows = list(reader)
    expected = [item.request_id for item in context.evaluation_requests]
    actual = [row.get("request_id", "") for row in rows]
    if actual != expected:
        raise InvalidInputError("Output request IDs must exactly match evaluation order.", context={
            "expected_count": len(expected), "actual_count": len(actual),
        })
    decisions = tuple(
        decision_from_csv_row(row, context.requests_by_id[row["request_id"]]) for row in rows
    )
    for row, decision in zip(rows, decisions, strict=True):
        if row != decision_to_csv_row(decision):
            raise InvalidInputError(
                "Output row is not in canonical challenge format.",
                context={"request_id": decision.request_id},
            )
    return decisions


def validate_output(path: Path, context, service) -> OutputValidation:
    errors: list[str] = []
    try:
        decisions = read_output(path, context)
        with path.open("r", encoding="utf-8", newline="") as handle:
            source_rows = list(csv.DictReader(handle))
    except (OSError, InvalidInputError) as error:
        return OutputValidation(False, 0, (getattr(error, "code", "invalid_output") + ":" + str(error),))
    for decision, source_row in zip(decisions, source_rows, strict=True):
        try:
            result = service.decide(decision.request_id)
            service.validator.require_valid(
                decision, context.bundle(decision.request_id), result.assessment
            )
            canonical_row = decision_to_csv_row(decision)
            if source_row != canonical_row:
                errors.append(f"{decision.request_id}:noncanonical_serialization")
            if canonical_row != decision_to_csv_row(result.decision):
                errors.append(f"{decision.request_id}:deterministic_decision_mismatch")
        except (ValueError, InvalidInputError) as error:
            errors.append(f"{decision.request_id}:{error}")
    return OutputValidation(not errors, len(decisions), tuple(errors))


def write_output_atomic(path: Path, decisions, *, dataset_dir: Path) -> None:
    target = path.resolve()
    if target.is_relative_to(dataset_dir.resolve()):
        raise InvalidInputError("Generated predictions must not overwrite the dataset directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=target.parent, prefix=".output-", suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="raise")
        writer.writeheader()
        for decision in decisions:
            writer.writerow(decision_to_csv_row(decision))
    temporary.replace(target)

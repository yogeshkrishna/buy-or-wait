"""Short deterministic explanations grounded in verified decision facts."""

from __future__ import annotations

from buywait.domain.enums import PaymentMethod, SpendingChangeAction
from buywait.domain.money import format_plan_amount


def explain(bundle, assessment, winner) -> str:
    currency = bundle.profile.home_currency.value
    floor = format_plan_amount(bundle.profile.minimum_balance_to_keep.amount)
    deadline = bundle.request.desired_completion_date.isoformat()
    if winner is None:
        if assessment.timeline.unresolved_event_ids:
            return "Do not proceed: mandatory financial evidence is unresolved, so no missing amount was assumed."
        return (
            f"Do not proceed: no accepted plan completes by {deadline} while preserving the "
            f"{currency} {floor} minimum balance for 90 days."
        )
    minimum = format_plan_amount(winner.safety_result.minimum_projected_balance.amount)
    prefix = _changes(winner.spending_changes, currency, bundle)
    if winner.payment_method is PaymentMethod.FULL_PAYMENT:
        amount = format_plan_amount(winner.payments[0].amount.amount)
        action = f"pay {currency} {amount} today"
    elif winner.payment_method is PaymentMethod.PARTIAL_PAYMENT:
        first, second = winner.payments
        action = (
            f"pay {currency} {format_plan_amount(first.amount.amount)} today and "
            f"{currency} {format_plan_amount(second.amount.amount)} on {second.payment_date.isoformat()}"
        )
    elif winner.payment_method is PaymentMethod.INSTALLMENTS:
        action = (
            f"use {len(winner.payments)} installments from {winner.payments[0].payment_date.isoformat()} "
            f"with total payable {currency} {format_plan_amount(winner.total_payable_amount.amount)}"
        )
    else:
        action = f"wait until {winner.payments[0].payment_date.isoformat()} and pay in full"
    raw = prefix + action
    sentence = raw[:1].upper() + raw[1:]
    return f"{sentence}. The verified balance stays at or above {currency} {minimum}, protecting the {currency} {floor} floor."


def _changes(changes, currency, bundle):
    if not changes:
        return ""
    events = {item.event_id: item for item in bundle.events}
    phrases = []
    for change in changes:
        event = events.get(change.event_id)
        label = event.category.replace("_", " ") if event else "flexible expense"
        if change.action is SpendingChangeAction.STOP:
            phrases.append(f"stop {label} ({change.event_id})")
        else:
            phrases.append(
                f"reduce {label} ({change.event_id}) to {currency} "
                f"{format_plan_amount(change.new_amount.amount)}"
            )
    return ", ".join(phrases) + ", then "

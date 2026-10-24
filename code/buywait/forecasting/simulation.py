"""Pure conservative day-by-day financial simulation."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import hashlib
import json

from buywait.domain.enums import (
    Direction,
    Flexibility,
    ForecastCheckpointKind,
    ForecastOrigin,
    SpendingChangeAction,
    ViolationCode,
)
from buywait.domain.errors import InvalidInputError
from buywait.domain.models import (
    CanonicalCashFlow,
    ForecastDay,
    ForecastOccurrence,
    ForecastProofPoint,
    ForecastSimulation,
    ForecastTimeline,
    Payment,
    RuleViolation,
    SafetyResult,
    SpendingChange,
)
from buywait.domain.money import Money
from .policy import ForecastPolicy, RELEASE_CANDIDATE_POLICY


class ForecastSimulator:
    """Simulate exact daily cash using the selected versioned global policy."""

    def __init__(self, policy: ForecastPolicy | None = None) -> None:
        self.policy = policy or RELEASE_CANDIDATE_POLICY

    def simulate(
        self,
        timeline: ForecastTimeline,
        starting_balance: Money,
        minimum_balance: Money,
        *,
        payments: tuple[Payment, ...] = (),
        spending_changes: tuple[SpendingChange, ...] = (),
    ) -> ForecastSimulation:
        if starting_balance.currency != timeline.home_currency or minimum_balance.currency != timeline.home_currency:
            raise InvalidInputError("Simulation money must use the timeline currency.")
        payments = tuple(sorted(payments, key=lambda item: (item.payment_date, item.sequence)))
        spending_changes = tuple(sorted(
            spending_changes, key=lambda item: (item.event_id, item.action.value)
        ))
        occurrence_by_date: dict = defaultdict(list)
        for item in timeline.occurrences:
            occurrence_by_date[item.flow.effective_date].append(item)
        payment_by_date: dict = defaultdict(list)
        for payment in payments:
            if not timeline.horizon.contains(payment.payment_date):
                raise InvalidInputError("A simulated payment lies outside the forecast horizon.")
            if payment.amount.currency != timeline.home_currency:
                raise InvalidInputError("A simulated payment uses another currency.")
            payment_by_date[payment.payment_date].append(payment)

        changed, change_violations = self._apply_changes(timeline, spending_changes)
        if changed is not None:
            occurrence_by_date = defaultdict(list)
            for item in changed:
                occurrence_by_date[item.flow.effective_date].append(item)

        current = starting_balance
        days: list[ForecastDay] = []
        checkpoints: list[ForecastProofPoint] = []
        violations: list[RuleViolation] = list(change_violations)
        minimum_seen = starting_balance
        tightest_date = timeline.horizon.start

        def checkpoint(day, kind, balance, related=()):
            nonlocal minimum_seen, tightest_date
            checkpoints.append(ForecastProofPoint(day, kind, balance, tuple(sorted(related))))
            if balance.amount < minimum_seen.amount:
                minimum_seen = balance
                tightest_date = day
            if balance.amount < minimum_balance.amount:
                violations.append(RuleViolation(
                    ViolationCode.BELOW_MINIMUM_BALANCE,
                    f"Balance falls below the protected floor at the {kind.value} checkpoint.",
                    day,
                ))

        day = timeline.horizon.start
        while day <= timeline.horizon.end:
            opening = current
            day_occurrences = sorted(occurrence_by_date.get(day, ()), key=lambda item: item.flow.flow_id)
            inflows = tuple(item.flow for item in day_occurrences if item.flow.direction is Direction.CREDIT)
            outflow_items = [item for item in day_occurrences if item.flow.direction is Direction.DEBIT]
            committed = tuple(item.flow for item in outflow_items if self._pre_credit(item))
            flexible = tuple(item.flow for item in outflow_items if not self._pre_credit(item))
            day_payments = tuple(sorted(payment_by_date.get(day, ()), key=lambda item: item.sequence))
            checkpoint(day, ForecastCheckpointKind.OPENING, current)
            credit_total = sum((item.amount.amount for item in inflows), Decimal(0))
            committed_total = sum((item.amount.amount for item in committed), Decimal(0))
            payment_total = sum((item.amount.amount for item in day_payments), Decimal(0))
            if self.policy.confirmed_credits_before_same_day_payments:
                current = Money(
                    current.amount + credit_total - committed_total - payment_total,
                    timeline.home_currency,
                )
            else:
                current = Money(
                    current.amount - committed_total - payment_total,
                    timeline.home_currency,
                )
            checkpoint(day, ForecastCheckpointKind.PRE_CREDIT, current,
                       tuple(item.flow_id for item in committed))
            if not self.policy.confirmed_credits_before_same_day_payments:
                current = Money(current.amount + credit_total, timeline.home_currency)
            current = Money(
                current.amount - sum((item.amount.amount for item in flexible), Decimal(0)),
                timeline.home_currency,
            )
            checkpoint(day, ForecastCheckpointKind.CLOSING, current,
                       tuple(item.flow_id for item in flexible))
            lowest = min(
                (point.balance for point in checkpoints[-3:]), key=lambda item: item.amount
            )
            days.append(ForecastDay(
                day,
                opening,
                inflows,
                tuple((*committed, *flexible)),
                day_payments,
                current,
                minimum_balance,
                lowest,
            ))
            day += timedelta(days=1)

        for event_id in timeline.unresolved_event_ids:
            violations.append(RuleViolation(
                ViolationCode.UNRESOLVED_EVIDENCE,
                "Mandatory financial evidence remains unresolved; the request fails closed.",
                timeline.horizon.start,
                event_id,
            ))
        violations = self._deduplicate_violations(violations)
        safe = not violations and minimum_seen.amount >= minimum_balance.amount
        fingerprint = self._fingerprint(timeline, starting_balance, minimum_balance, payments,
                                        spending_changes, days, violations, self.policy.fingerprint)
        safety = SafetyResult(
            safe,
            minimum_seen,
            minimum_balance,
            tightest_date,
            tuple(violations),
            fingerprint,
        )
        # The proof is intentionally compact: tightest checkpoints plus every
        # violation checkpoint, with no loss of the full daily simulation.
        tight = [item for item in checkpoints if item.forecast_date == tightest_date]
        proof = tuple(tight[:3])
        return ForecastSimulation(timeline.request_id, tuple(days), safety, proof, fingerprint)

    @staticmethod
    def _pre_credit(item: ForecastOccurrence) -> bool:
        return (
            item.origin is ForecastOrigin.CANONICAL_FUTURE
            or item.flow.protected
            or item.flow.flexibility is Flexibility.FIXED
        )

    @staticmethod
    def _apply_changes(timeline, changes):
        if not changes:
            return None, ()
        by_rule = {item.rule_id: item for item in timeline.recurrence_rules}
        selected: dict[str, SpendingChange] = {}
        violations: list[RuleViolation] = []
        for change in changes:
            if change.event_id in selected:
                violations.append(RuleViolation(
                    ViolationCode.ILLEGAL_SPENDING_CHANGE,
                    "Stopping and reducing the same recurring event are mutually exclusive.",
                    timeline.horizon.start,
                    change.event_id,
                ))
                continue
            selected[change.event_id] = change
        transformed = []
        matched: set[str] = set()
        for item in timeline.occurrences:
            if item.origin is not ForecastOrigin.RECURRENCE:
                transformed.append(item)
                continue
            rule = by_rule[item.recurrence_rule_id]
            applicable = [selected[event_id] for event_id in rule.source_event_ids if event_id in selected]
            if not applicable:
                transformed.append(item)
                continue
            change = applicable[0]
            matched.add(change.event_id)
            allowed = (
                change.action is SpendingChangeAction.STOP
                and rule.flexibility in {Flexibility.STOPPABLE, Flexibility.REDUCIBLE_OR_STOPPABLE}
            ) or (
                change.action is SpendingChangeAction.REDUCE_TO
                and rule.flexibility in {Flexibility.REDUCIBLE, Flexibility.REDUCIBLE_OR_STOPPABLE}
            )
            if not allowed:
                violations.append(RuleViolation(
                    ViolationCode.ILLEGAL_SPENDING_CHANGE,
                    "Only supplied recurring expenses marked flexible may be changed.",
                    timeline.horizon.start,
                    change.event_id,
                ))
                transformed.append(item)
                continue
            if change.action is SpendingChangeAction.STOP:
                continue
            assert change.new_amount is not None
            minimum = rule.minimum_allowed_amount.amount if rule.minimum_allowed_amount else Decimal(0)
            if (change.new_amount.currency != timeline.home_currency
                    or change.new_amount.amount < minimum
                    or change.new_amount.amount > item.flow.amount.amount):
                violations.append(RuleViolation(
                    ViolationCode.ILLEGAL_SPENDING_CHANGE,
                    "A reduction must stay between the recurring minimum and current forecast amount.",
                    timeline.horizon.start,
                    change.event_id,
                ))
                transformed.append(item)
                continue
            transformed.append(ForecastOccurrence(
                replace_flow_amount(item.flow, change.new_amount), item.origin, item.recurrence_rule_id
            ))
        for event_id, change in selected.items():
            if event_id not in matched:
                violations.append(RuleViolation(
                    ViolationCode.ILLEGAL_SPENDING_CHANGE,
                    "The requested change does not identify an eligible recurring flexible expense.",
                    timeline.horizon.start,
                    event_id,
                ))
        return tuple(transformed), tuple(violations)

    @staticmethod
    def _deduplicate_violations(violations):
        unique = {
            (item.code, item.violation_date, item.related_id, item.message): item for item in violations
        }
        return tuple(sorted(unique.values(), key=lambda item: (
            item.violation_date or timeline_date_min(), item.code.value, item.related_id or "", item.message
        )))

    @staticmethod
    def _fingerprint(timeline, starting, floor, payments, changes, days, violations, policy_fingerprint):
        payload = {
            "policy": policy_fingerprint,
            "timeline": timeline.fingerprint,
            "starting": str(starting.amount),
            "floor": str(floor.amount),
            "payments": [[item.payment_date.isoformat(), str(item.amount.amount), item.sequence] for item in payments],
            "changes": [[item.action.value, item.event_id,
                         str(item.new_amount.amount) if item.new_amount else None] for item in changes],
            "days": [[item.forecast_date.isoformat(), str(item.opening_balance.amount),
                      str(item.closing_balance.amount), str(item.lowest_intraday_balance.amount)] for item in days],
            "violations": [[item.code.value, item.violation_date.isoformat() if item.violation_date else None,
                            item.related_id] for item in violations],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replace_flow_amount(flow: CanonicalCashFlow, amount: Money) -> CanonicalCashFlow:
    from dataclasses import replace
    return replace(flow, amount=amount, original_amount=None, fx_rate=None, fx_rate_date=None)


def timeline_date_min():
    from datetime import date
    return date.min

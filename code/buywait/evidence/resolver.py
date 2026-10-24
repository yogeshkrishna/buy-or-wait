"""Conservative evidence merge. No ledger mutation or affordability decisions."""
from collections import defaultdict
from decimal import Decimal
from buywait.domain.enums import Direction
from .types import EvidenceConflict


def resolve(facts, *, directions=None):
    directions = directions or {}
    groups = defaultdict(list)
    for fact in facts:
        # Different effective dates are temporal amendments, not contradictory
        # observations of the same instant. Phase 05 applies the date timeline.
        family = "state" if fact.fact_type.value in {"status", "cash_state", "settlement", "cancellation"} else fact.fact_type.value
        groups[(fact.related_event_id or fact.subject, fact.subject, family, fact.scope, fact.effective_date)].append(fact)
    selected, conflicts = [], []
    for group in groups.values():
        distinct = {fact.fact_id: fact for fact in group}
        candidates = list(distinct.values())
        if len({(str(f.value), f.currency) for f in candidates}) <= 1:
            selected.append(sorted(candidates, key=lambda f: (-f.confidence, f.fact_id))[0])
            continue
        original = candidates[:]
        explicit = [f for f in candidates if f.explicit]
        if explicit:
            candidates = explicit
        reason = "explicit_amendment_or_terminal_state"
        # Newer only within a demonstrably identical source identity.
        by_source = defaultdict(list)
        for fact in candidates:
            by_source[fact.provenance.source_identity or fact.provenance.source_id].append(fact)
        candidates = []
        for siblings in by_source.values():
            timed = [f for f in siblings if f.provenance.observed_at is not None]
            if len(timed) == len(siblings) and len(siblings) > 1:
                latest = max(f.provenance.observed_at for f in timed)
                candidates.extend(f for f in siblings if f.provenance.observed_at == latest)
                reason = "newer_same_source"
            else:
                candidates.extend(siblings)
        if len(candidates) > 1:
            settled = [f for f in candidates if f.value == "settled"]
            if settled:
                candidates = settled
                reason = "settled_over_estimate"
        if len(candidates) > 1:
            currencies = {f.currency for f in candidates}
            if all(isinstance(f.value, Decimal) for f in candidates) and len(currencies) == 1:
                direction = directions.get(candidates[0].related_event_id)
                if direction is None and candidates[0].subject in {"salary", "bonus", "commission", "payout", "prize"}:
                    direction = Direction.CREDIT
                if direction in {Direction.DEBIT, Direction.CREDIT}:
                    value = (max if direction == Direction.DEBIT else min)(f.value for f in candidates)
                    candidates = [f for f in candidates if f.value == value]
                    reason = "financially_safer_amount"
            else:
                pending = [f for f in candidates if f.value in {"pending_credit", "unrealized", "income_ended", "outstanding_debit"}]
                if pending:
                    candidates = pending
                    reason = "financially_safer_state"
        unambiguous = len({(str(f.value), f.currency) for f in candidates}) == 1
        chosen = sorted(candidates, key=lambda f: f.fact_id)[0] if unambiguous else None
        conflicts.append(EvidenceConflict(tuple(sorted(f.fact_id for f in original)), chosen.fact_id if chosen else None,
                                          reason if chosen else "unresolved_disagreement"))
        if chosen:
            selected.append(chosen)
    return tuple(sorted(selected, key=lambda f: f.fact_id)), tuple(conflicts)

"""Defensive event-link graph construction and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from buywait.domain.models import LedgerWarning, RawEvent


@dataclass(frozen=True, slots=True)
class LifecycleComponent:
    lifecycle_id: str
    event_ids: tuple[str, ...]
    invalid: bool = False


def _identifier(user_id: str, event_ids: tuple[str, ...]) -> str:
    payload = user_id + "\0" + "\0".join(event_ids)
    return "lifecycle_" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def build_components(events: tuple[RawEvent, ...]) -> tuple[tuple[LifecycleComponent, ...], tuple[LedgerWarning, ...]]:
    """Build undirected lifecycle components while retaining invalid rows for audit."""
    by_id = {event.event_id: event for event in events}
    adjacency = {event.event_id: set() for event in events}
    invalid_ids: set[str] = set()
    warnings: list[LedgerWarning] = []

    for event in events:
        parent_id = event.linked_event_id
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            invalid_ids.add(event.event_id)
            warnings.append(LedgerWarning(
                "missing_lifecycle_parent",
                "A linked event references a parent absent from this user's ledger; the child remains unresolved.",
                (event.event_id, parent_id),
            ))
            continue
        if parent.user_id != event.user_id:
            invalid_ids.add(event.event_id)
            warnings.append(LedgerWarning(
                "cross_user_lifecycle_link",
                "A lifecycle link crosses user boundaries and was quarantined.",
                (event.event_id, parent_id),
            ))
            continue
        adjacency[event.event_id].add(parent_id)
        adjacency[parent_id].add(event.event_id)

    # linked_event_id is a parent pointer, so a three-color walk detects every cycle.
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(event_id: str) -> None:
        color[event_id] = 1
        stack.append(event_id)
        parent_id = by_id[event_id].linked_event_id
        if parent_id in by_id and by_id[parent_id].user_id == by_id[event_id].user_id:
            if color.get(parent_id, 0) == 0:
                visit(parent_id)
            elif color.get(parent_id) == 1:
                start = stack.index(parent_id)
                cycle = tuple(sorted(stack[start:]))
                invalid_ids.update(cycle)
                warnings.append(LedgerWarning(
                    "lifecycle_cycle",
                    "A lifecycle cycle was quarantined instead of selecting an arbitrary canonical event.",
                    cycle,
                ))
        stack.pop()
        color[event_id] = 2

    for event_id in sorted(by_id):
        if color.get(event_id, 0) == 0:
            visit(event_id)

    components: list[LifecycleComponent] = []
    seen: set[str] = set()
    for start in sorted(by_id):
        if start in seen:
            continue
        pending = [start]
        members: set[str] = set()
        while pending:
            current = pending.pop()
            if current in members:
                continue
            members.add(current)
            pending.extend(adjacency[current] - members)
        seen.update(members)
        event_ids = tuple(sorted(members))
        components.append(LifecycleComponent(
            _identifier(by_id[start].user_id, event_ids), event_ids, bool(set(event_ids) & invalid_ids)
        ))
    warnings.sort(key=lambda item: (item.code, item.related_ids))
    return tuple(components), tuple(warnings)

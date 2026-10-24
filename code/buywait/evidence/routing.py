from __future__ import annotations

from datetime import datetime, time, timezone
import hashlib
import re

from buywait.domain.enums import EvidenceSourceKind
from buywait.domain.models import EvaluationBundle
from .types import EvidenceIssue, EvidenceSource


def _identity(user_id: str, source_type: str, text: str, source_id: str) -> str:
    patterns = (
        r"(?:from| at | di )\s*([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3})(?=[.,]| payroll| about| has| telah)",
        r"^([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3}) (?:has|wanted)",
    )
    entity = next((match.group(1).strip().lower() for pattern in patterns if (match := re.search(pattern, text))), None)
    identity = f"{user_id}:{source_type}:{entity or source_id}"
    return hashlib.sha256(identity.encode()).hexdigest()


def route(bundle: EvaluationBundle) -> tuple[tuple[EvidenceSource, ...], tuple[EvidenceIssue, ...]]:
    events = {event.event_id: event for event in bundle.events}
    cutoff = datetime.combine(bundle.request.request_date, time.max, timezone.utc)
    sources, issues, seen = [], [], set()
    for item in (*bundle.messages, *bundle.images):
        is_message = hasattr(item, "message_id")
        source_id = item.message_id if is_message else item.image_id
        kind = EvidenceSourceKind.MESSAGE if is_message else EvidenceSourceKind.IMAGE
        if (kind, source_id) in seen:
            continue
        seen.add((kind, source_id))
        if item.user_id != bundle.profile.user_id:
            issues.append(EvidenceIssue(source_id, "cross_user_evidence"))
            continue
        event = events.get(item.related_event_id)
        priority = 3 if event else 2 if item.request_id == bundle.request.request_id else 1
        if item.related_event_id is not None and event is None:
            issues.append(EvidenceIssue(source_id, "unlinked_event"))
            continue
        if event is None and item.request_id not in {None, bundle.request.request_id}:
            issues.append(EvidenceIssue(source_id, "other_request_evidence"))
            continue
        if is_message and item.sent_at > cutoff:
            issues.append(EvidenceIssue(source_id, "future_message_excluded", item.related_event_id))
            continue
        subject = "salary" if event and event.category == "salary" else "event"
        sources.append(EvidenceSource(
            source_id, kind, item.user_id, bundle.request.request_id, item.related_event_id,
            item.sent_at if is_message else None,
            _identity(item.user_id, item.source_type.value, item.message_text, source_id) if is_message else f"image:{source_id}",
            event.currency if event else bundle.profile.home_currency, event.direction if event else None,
            subject, event.description if event else "",
            hashlib.sha256(item.message_text.encode()).hexdigest() if is_message else "",
            priority, item.message_text if is_message else "",
            bundle.request.request_date, event.settlement_date if event else None,
        ))
    return tuple(sorted(sources, key=lambda item: (-item.priority, item.source_id))), tuple(issues)

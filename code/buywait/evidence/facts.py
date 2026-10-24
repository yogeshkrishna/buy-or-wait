from datetime import date
from decimal import Decimal
import hashlib
import json

from buywait.domain.enums import EvidenceFactType
from buywait.domain.models import EvidenceFact, EvidenceProvenance
from .types import EvidenceSource


def make_fact(source: EvidenceSource, kind: str, value: str | Decimal | date | bool | None, *,
              subject: str | None = None, scope: str = "one_time", effective_date: date | None = None,
              explicit: bool = False, confidence: str = "0.95", provider: str = "deterministic-v2",
              rule: str = "supported_statement", support_ref: str | None = None) -> EvidenceFact:
    identity = json.dumps([source.source_id, source.content_hash, kind, str(value), subject or source.subject,
                           scope, str(effective_date), explicit, provider, rule], separators=(",", ":"))
    return EvidenceFact(
        fact_id="fact_" + hashlib.sha256(identity.encode()).hexdigest()[:24], fact_type=EvidenceFactType(kind), value=value,
        provenance=EvidenceProvenance(source.kind, source.source_id, source.observed_at,
                                      source.content_hash, provider, schema_version="buywait.evidence.v2", source_identity=source.source_identity),
        related_event_id=source.related_event_id, effective_date=effective_date, currency=source.currency,
        confidence=Decimal(confidence), notes=(rule,), subject=subject or source.subject, scope=scope,
        explicit=explicit, support_ref=support_ref,
    )

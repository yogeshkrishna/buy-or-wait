from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from buywait.domain.enums import Currency, Direction, EvidenceSourceKind
from buywait.domain.models import EvidenceFact, UsageRecord

SCHEMA_VERSION = "buywait.evidence-extraction.v2"
MODEL_SCHEMA_VERSION = "buywait.model-extraction.v1"
PROMPT_VERSION = "financial-facts-v3"
SUBJECTS = ("event", "salary", "bonus", "commission", "arrears", "rent", "childcare", "payout", "refund",
            "transfer", "investment", "prize", "service", "expense")
SCOPES = ("one_time", "recurring", "next_cycle", "historical")


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    source_id: str
    kind: EvidenceSourceKind
    user_id: str
    request_id: str
    related_event_id: str | None
    observed_at: datetime | None
    source_identity: str
    currency: Currency
    direction: Direction | None
    subject: str
    description: str
    content_hash: str
    priority: int
    text: str = field(default="", repr=False)
    request_date: date | None = None
    settlement_date: date | None = None


@dataclass(frozen=True, slots=True)
class EvidenceIssue:
    source_id: str
    reason_code: str
    related_event_id: str | None = None
    mandatory: bool = False


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class OcrDocument:
    text: str = field(repr=False)
    words: tuple[OcrWord, ...] = field(repr=False)
    provider: str
    model: str
    language: str
    width: int
    height: int


class OcrProvider(Protocol):
    provider_id: str
    version: str

    def recognize(self, image: bytes) -> OcrDocument: ...


@dataclass(frozen=True, slots=True)
class Extraction:
    facts: tuple[EvidenceFact, ...] = ()
    issues: tuple[EvidenceIssue, ...] = ()
    usage: tuple[UsageRecord, ...] = ()
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceConflict:
    fact_ids: tuple[str, ...]
    selected_fact_id: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    request_id: str
    facts: tuple[EvidenceFact, ...]
    issues: tuple[EvidenceIssue, ...]
    conflicts: tuple[EvidenceConflict, ...]
    usage: tuple[UsageRecord, ...]
    mandatory_event_ids: tuple[str, ...]
    resolved_event_ids: tuple[str, ...]
    cache_hits: int = 0

    @property
    def ready(self) -> bool:
        return set(self.mandatory_event_ids) <= set(self.resolved_event_ids)

    def to_private_dict(self) -> dict:
        """Typed full-fidelity wire projection. Never use as ordinary CLI/log output."""
        import json
        result = asdict(self)
        result.pop("usage")  # Usage has its own append-only accounting boundary.
        result["schema_version"] = SCHEMA_VERSION
        return json.loads(json.dumps(result, default=lambda value: value.isoformat() if isinstance(value, (date, datetime)) else str(value)))

    def redacted(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION, "request_id": self.request_id, "ready": self.ready,
            "mandatory_count": len(self.mandatory_event_ids), "resolved_count": len(self.resolved_event_ids),
            "facts": [{"fact_id": fact.fact_id, "source_id": fact.provenance.source_id,
                       "related_event_id": fact.related_event_id, "fact_type": fact.fact_type.value,
                       "subject": fact.subject, "scope": fact.scope, "confidence": str(fact.confidence),
                       "provider": fact.provenance.provider, "content_hash": fact.provenance.content_hash,
                       "value": "[REDACTED]"} for fact in self.facts],
            "issues": [{"source_id": issue.source_id, "reason_code": issue.reason_code,
                        "related_event_id": issue.related_event_id, "mandatory": issue.mandatory} for issue in self.issues],
            "conflicts": [{"fact_ids": list(item.fact_ids), "selected_fact_id": item.selected_fact_id,
                           "reason_code": item.reason_code} for item in self.conflicts],
            "usage": {"model_calls": len([item for item in self.usage if not item.cache_hit]),
                      "unknown_usage_calls": sum("usage_unknown" in item.operation for item in self.usage),
                      "input_tokens": sum(item.input_tokens for item in self.usage),
                      "output_tokens": sum(item.output_tokens for item in self.usage),
                      "estimated_cost_usd": str(sum((item.estimated_cost_usd for item in self.usage), Decimal(0)))},
            "cache_hits": self.cache_hits,
        }

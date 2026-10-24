"""Stable dependency-inversion ports implemented by later outer adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from buywait.contracts.models import ProviderCapability
from buywait.domain.models import Decision, EvaluationBundle, EvidenceFact, UsageRecord


class EvidenceExtractor(Protocol):
    def extract(self, bundle: EvaluationBundle) -> tuple[EvidenceFact, ...]: ...


class DecisionEngine(Protocol):
    def decide(
        self, bundle: EvaluationBundle, evidence: Sequence[EvidenceFact]
    ) -> Decision: ...


class AuditSink(Protocol):
    def record(self, trace: Mapping[str, Any]) -> str: ...


class UsageSink(Protocol):
    def record(self, usage: UsageRecord) -> None: ...


class ProviderRegistry(Protocol):
    def capabilities(self) -> tuple[ProviderCapability, ...]: ...

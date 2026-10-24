"""Stable error taxonomy shared by CLI, API, and evaluation adapters."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class BuyWaitError(Exception):
    """Base exception with a stable machine-readable code and safe context."""

    default_code = "buywait_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.context = MappingProxyType(dict(context or {}))

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": dict(self.context)}


class InvalidInputError(BuyWaitError):
    default_code = "invalid_input"


class UnresolvedEvidenceError(BuyWaitError):
    default_code = "unresolved_evidence"


class UnsupportedCurrencyError(BuyWaitError):
    default_code = "unsupported_currency"


class MissingExchangeRateError(BuyWaitError):
    default_code = "missing_exchange_rate"


class UnsafeCandidateError(BuyWaitError):
    default_code = "unsafe_candidate"


class ProviderUnavailableError(BuyWaitError):
    default_code = "provider_unavailable"


class InvariantViolationError(BuyWaitError):
    default_code = "invariant_violation"


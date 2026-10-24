"""Provider metadata is an application contract, not a financial domain concept."""

from dataclasses import dataclass

from buywait.domain.enums import ProviderKind
from buywait.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider_id: str
    kind: ProviderKind
    provider_version: str
    available: bool
    configured: bool
    operations: tuple[str, ...]
    schema_versions: tuple[str, ...]
    requires_network: bool = False
    health_detail: str | None = None
    schema_version: str = "buywait.provider-capability.v1"

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.provider_version.strip():
            raise InvalidInputError("Provider id and version must be nonblank.")
        if len(set(self.operations)) != len(self.operations):
            raise InvalidInputError("Provider operations must be unique.")
        if self.schema_version != "buywait.provider-capability.v1":
            raise InvalidInputError("Unsupported provider-capability schema version.")

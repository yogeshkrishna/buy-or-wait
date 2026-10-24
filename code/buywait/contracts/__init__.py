"""Versioned boundary contracts consumed by application adapters."""

from .models import ProviderCapability
from .schema_registry import SCHEMA_FILES, SCHEMA_VERSIONS

__all__ = ["ProviderCapability", "SCHEMA_FILES", "SCHEMA_VERSIONS"]

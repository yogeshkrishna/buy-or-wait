"""Privacy-safe, source-located validation diagnostics."""

from dataclasses import asdict, dataclass
from typing import Any

from buywait.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class SourceLocation:
    filename: str
    record_number: int
    line_number: int
    record_id: str = ""


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    source: SourceLocation
    field: str = ""
    severity: str = "error"
    key: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DatasetValidationError(InvalidInputError):
    """All issues from the failed stage; callers must not use a partial dataset."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__(
            "Dataset validation failed. Correct the indicated file, row, and field.",
            context={"error_count": len(issues)},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "error_count": len(self.issues),
            "issues": [issue.as_dict() for issue in self.issues[:100]],
            "issues_truncated": len(self.issues) > 100,
        }

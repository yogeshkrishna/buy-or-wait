"""Strict UTF-8 CSV and scalar parsers; diagnostics never echo cell contents."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping
from typing import Literal, TypeVar, overload

from buywait.domain.enums import Currency
from buywait.domain.money import Money

from .diagnostics import SourceLocation, ValidationIssue

E = TypeVar("E", bound=StrEnum)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")


class CellError(Exception):
    def __init__(self, field: str, message: str, code: str = "invalid_input") -> None:
        self.field, self.message, self.code = field, message, code


@dataclass(frozen=True, slots=True)
class CsvRow:
    source: SourceLocation
    values: Mapping[str, str]

    def text(self, field: str, *, optional: bool = False) -> str:
        value = self.values[field].strip()
        if not value and not optional:
            raise CellError(field, "A nonblank value is required.")
        return value

    @overload
    def identifier(self, field: str, *, optional: Literal[False] = False) -> str: ...

    @overload
    def identifier(self, field: str, *, optional: Literal[True]) -> str | None: ...

    def identifier(self, field: str, *, optional: bool = False) -> str | None:
        value = self.text(field, optional=optional)
        if not value and optional:
            return None
        if not IDENTIFIER.fullmatch(value):
            raise CellError(field, "Use an identifier containing letters, digits, underscores, dots or hyphens.")
        return value

    def enum(self, field: str, kind: type[E]) -> E:
        try:
            return kind(self.text(field))
        except ValueError as error:
            code = "unsupported_currency" if kind is Currency else "invalid_input"
            raise CellError(field, "Expected one of: " + ", ".join(item.value for item in kind), code) from error

    def number(self, field: str) -> Decimal:
        value = self.text(field)
        if not NUMBER.fullmatch(value) or len(value) > 100:
            raise CellError(field, "Expected a finite plain decimal without commas, exponents or currency symbols.")
        return Decimal(value)

    @overload
    def money(self, field: str, currency: Currency, *, optional: Literal[False] = False) -> Money: ...

    @overload
    def money(self, field: str, currency: Currency, *, optional: Literal[True]) -> Money | None: ...

    def money(self, field: str, currency: Currency, *, optional: bool = False) -> Money | None:
        if optional and not self.text(field, optional=True):
            return None
        value = self.number(field)
        if value < 0:
            raise CellError(field, "Amount must be nonnegative.")
        return Money(value, currency)

    @overload
    def integer(self, field: str, *, optional: Literal[False] = False) -> int: ...

    @overload
    def integer(self, field: str, *, optional: Literal[True]) -> int | None: ...

    def integer(self, field: str, *, optional: bool = False) -> int | None:
        value = self.text(field, optional=optional)
        if not value and optional:
            return None
        if not re.fullmatch(r"[0-9]{1,9}", value) or int(value) <= 0:
            raise CellError(field, "Expected a positive integer.")
        return int(value)

    def boolean(self, field: str) -> bool:
        value = self.text(field).lower()
        if value in {"true", "yes", "1"}:
            return True
        if value in {"false", "no", "0"}:
            return False
        raise CellError(field, "Expected true/false, yes/no or 1/0.")

    @overload
    def day(self, field: str, *, optional: Literal[False] = False) -> date: ...

    @overload
    def day(self, field: str, *, optional: Literal[True]) -> date | None: ...

    def day(self, field: str, *, optional: bool = False) -> date | None:
        value = self.text(field, optional=optional)
        if not value and optional:
            return None
        try:
            if not DATE.fullmatch(value):
                raise ValueError
            return date.fromisoformat(value)
        except ValueError as error:
            raise CellError(field, "Expected a real calendar date in YYYY-MM-DD format.") from error

    def timestamp(self, field: str) -> datetime:
        value = self.text(field)
        try:
            if not TIMESTAMP.fullmatch(value):
                raise ValueError
            return datetime.fromisoformat(value).astimezone(timezone.utc)
        except ValueError as error:
            raise CellError(field, "Expected an ISO timestamp with time zone, such as 2026-09-01T09:30:00Z.") from error

    def items(self, field: str, *, optional: bool = True) -> tuple[str, ...]:
        value = self.text(field, optional=optional)
        if not value:
            return ()
        parts = tuple(item.strip() for item in value.split("|"))
        if not all(parts) or len(set(parts)) != len(parts):
            raise CellError(field, "Pipe-separated lists cannot contain empty or repeated items.")
        return parts


def read_csv(path: Path, header: tuple[str, ...]) -> tuple[tuple[CsvRow, ...], tuple[ValidationIssue, ...]]:
    rows: list[CsvRow] = []
    issues: list[ValidationIssue] = []
    location = SourceLocation(path.name, 0, 1)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            if tuple(next(reader, ())) != header:
                return (), (ValidationIssue("invalid_header", "Expected exact header order: " + ",".join(header), location),)
            record_number = 0
            while True:
                first_line = reader.line_num + 1
                try:
                    cells = next(reader)
                except StopIteration:
                    break
                record_number += 1
                # Only syntactically safe row IDs enter diagnostics.
                identifier = cells[0].strip() if cells else ""
                if not IDENTIFIER.fullmatch(identifier):
                    identifier = ""
                location = SourceLocation(path.name, record_number, first_line, identifier)
                if len(cells) != len(header):
                    issues.append(ValidationIssue("invalid_row_width", "Row width does not match the header.", location))
                    continue
                rows.append(CsvRow(location, MappingProxyType(dict(zip(header, cells)))))
    except (OSError, UnicodeError, csv.Error) as error:
        code = "file_unavailable" if isinstance(error, OSError) else "invalid_csv"
        issues.append(ValidationIssue(code, "Cannot read file as strict UTF-8 CSV.", location))
    return tuple(rows), tuple(issues)

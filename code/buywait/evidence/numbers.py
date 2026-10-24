"""Localized number parsing with narrow, auditable OCR corrections."""

from decimal import Decimal
import re

from buywait.domain.errors import UnresolvedEvidenceError


def financial_number(token: str, *, ocr: bool = False) -> Decimal:
    value = token.strip().replace("\u00a0", " ")
    if ocr and " " in value:
        # Spaces are grouping separators only when the complete token is a
        # western/Indian grouped monetary column, never between unrelated cells.
        candidate = value.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}))
        integer = candidate.split(".")[0]
        if _grouped(integer, " ") and re.fullmatch(r"[0-9 ]+(?:\.[0-9]{1,2})?", candidate):
            value = candidate.replace(" ", "")
    if ocr and re.fullmatch(r"[0-9OoIl|.,]+", value) and re.search(r"[0-9]", value):
        value = value.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}))
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        return Decimal(value)
    # Accept western and Indian digit grouping; a trailing 1-2 digit group is decimals.
    if "," in value and "." in value:
        decimal_separator = "." if value.rfind(".") > value.rfind(",") else ","
        grouping_separator = "," if decimal_separator == "." else "."
        integer, fraction = value.rsplit(decimal_separator, 1)
        if not re.fullmatch(r"[0-9]{1,2}", fraction):
            raise UnresolvedEvidenceError("Ambiguous decimal grouping.")
        if not _grouped(integer, grouping_separator):
            raise UnresolvedEvidenceError("Invalid thousands grouping.")
        return Decimal(integer.replace(grouping_separator, "") + "." + fraction)
    for separator in (",", "."):
        if separator in value:
            if _grouped(value, separator):
                return Decimal(value.replace(separator, ""))
            parts = value.split(separator)
            if len(parts) == 2 and parts[0].isdigit() and re.fullmatch(r"[0-9]{1,2}", parts[1]):
                return Decimal(parts[0] + "." + parts[1])
    raise UnresolvedEvidenceError("Unsupported or ambiguous monetary token.")


def _grouped(value: str, separator: str) -> bool:
    parts = value.split(separator)
    if len(parts) < 2 or not all(part.isdigit() for part in parts) or len(parts[-1]) != 3:
        return False
    return (1 <= len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:])) or (
        1 <= len(parts[0]) <= 2 and all(len(part) == 2 for part in parts[1:-1]))

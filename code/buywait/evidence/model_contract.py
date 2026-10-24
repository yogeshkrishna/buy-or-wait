"""Closed extraction wire contract and validator, shared by model and cache boundaries."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from buywait.domain.errors import UnresolvedEvidenceError
from .facts import make_fact
from .safety import blocked_content, normalized_text
from .types import MODEL_SCHEMA_VERSION, SUBJECTS, SCOPES

KINDS = ("amount", "effective_date", "settlement_date", "status", "recurrence", "amendment", "cancellation", "settlement", "internal_transfer", "cash_state")
VALUES = ("confirmed", "pending", "settled", "cancelled", "failed", "unrealized", "pending_credit", "outstanding_debit",
          "regular", "nonrecurring", "internal", "income_ended")
PROPERTIES = {
    "fact_type": {"type": "string", "enum": list(KINDS)}, "subject": {"type": "string", "enum": list(SUBJECTS)},
    "scope": {"type": "string", "enum": list(SCOPES)}, "value": {"type": ["string", "null"]},
    "currency": {"type": ["string", "null"], "enum": ["EUR", "IDR", "INR", "USD", "ZAR", None]},
    "effective_date": {"type": ["string", "null"]}, "explicit": {"type": "boolean"},
    "confidence": {"type": "string"}, "supporting_quote": {"type": ["string", "null"]},
}
MODEL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["schema_version", "transcription", "facts"],
    "properties": {"schema_version": {"type": "string", "enum": [MODEL_SCHEMA_VERSION]},
                   "transcription": {"type": ["string", "null"]},
                   "facts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                             "required": list(PROPERTIES), "properties": PROPERTIES}}},
}


def typed_value(kind: str, value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 100 or blocked_content(value):
        raise ValueError("Invalid value")
    if kind == "amount":
        if not re.fullmatch(r"\d{1,16}(?:\.\d{1,6})?", value):
            raise ValueError("Amount must be an exact nonnegative decimal string")
        return Decimal(value)
    if kind in {"effective_date", "settlement_date"}:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Date must be ISO")
        return date.fromisoformat(value)
    if value in VALUES or kind == "amendment" and re.fullmatch(r"increase_percent:\d{1,3}(?:\.\d{1,2})?", value):
        return value
    raise ValueError("Unknown financial state")


def validate_model_output(payload, source, *, provider: str, ocr_text: str = ""):
    try:
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "transcription", "facts"} or payload["schema_version"] != MODEL_SCHEMA_VERSION:
            raise ValueError("Schema mismatch")
        transcript = payload["transcription"]
        if transcript is not None and (not isinstance(transcript, str) or len(transcript) > 30000 or blocked_content(transcript)):
            raise ValueError("Unsafe transcription")
        support = source.text or ocr_text or transcript or ""
        if blocked_content(support):
            raise ValueError("Unsafe evidence")
        if not isinstance(payload["facts"], list) or len(payload["facts"]) > 32:
            raise ValueError("Fact count")
        facts = []
        for item in payload["facts"]:
            if not isinstance(item, dict) or set(item) != set(PROPERTIES):
                raise ValueError("Unexpected output fields")
            if item["fact_type"] not in KINDS or item["subject"] not in SUBJECTS or item["scope"] not in SCOPES or type(item["explicit"]) is not bool:
                raise ValueError("Closed vocabulary mismatch")
            if item["currency"] not in {None, source.currency.value}:
                raise ValueError("Currency disagrees with linked record")
            if not isinstance(item["confidence"], str):
                raise ValueError("Confidence must be a decimal string")
            confidence = Decimal(item["confidence"])
            if not confidence.is_finite() or not 0 <= confidence <= 1:
                raise ValueError("Invalid confidence")
            value = typed_value(item["fact_type"], item["value"])
            effective = typed_value("effective_date", item["effective_date"])
            quote = item["supporting_quote"]
            if value is None:
                continue  # Null is missing evidence, never a zero-valued fact.
            if not isinstance(quote, str) or not quote.strip() or len(quote) > 400 or normalized_text(quote).casefold() not in normalized_text(support).casefold():
                raise ValueError("Missing financial support")
            if item["fact_type"] == "amount":
                from .numbers import financial_number
                tokens = re.findall(r"(?<!\w)\d[\d.,]*", quote)
                supported_values = []
                for token in tokens:
                    try:
                        supported_values.append(financial_number(token.rstrip(".,")))
                    except UnresolvedEvidenceError:
                        continue
                if value not in supported_values:
                    raise ValueError("Amount is not supported by its quote")
            facts.append(make_fact(source, item["fact_type"], value, subject=item["subject"], scope=item["scope"],
                                   effective_date=effective, explicit=item["explicit"], confidence=str(min(confidence, Decimal("0.85"))),
                                   provider=provider, rule="model_supported_financial_fact", support_ref="quote-sha256:" + __import__("hashlib").sha256(quote.encode()).hexdigest()))
        return tuple(facts)
    except (ValueError, TypeError, KeyError, InvalidOperation) as error:
        raise UnresolvedEvidenceError("Model output failed the closed financial-evidence schema.", code="invalid_model_evidence") from error

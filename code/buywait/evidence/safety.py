"""Conservative content gates, not executable text sanitization."""

import re
import unicodedata

INSTRUCTIONS = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|system|these)|disregard\s+(?:rules|instructions)|"
    r"system\s*(?:prompt|message|:)|developer\s*(?:message|:)|"
    r"amount_safe_to_pay|affordability_status|recommended_payment_method|"
    r"(?:run|execute)\s+(?:this|command|code)|os\.environ|api[_ -]?key|"
    r"abaikan\s+(?:semua|instruksi|aturan)|ubah\s+(?:aturan|rekomendasi)", re.I,
)
SECRETS = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                     r"(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+)", re.I)


def normalized_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("’", "'")


def blocked_content(text: str) -> bool:
    return bool(INSTRUCTIONS.search(normalized_text(text)) or SECRETS.search(text))


def redact_secrets(text: str) -> str:
    return SECRETS.sub("[REDACTED]", text)

"""Private reproducible cache; no raw messages, images, transcripts, or credentials."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
from buywait.domain.enums import Currency
from buywait.domain.models import UsageRecord
from buywait.domain.errors import BuyWaitError
from .facts import make_fact
from .model_contract import KINDS, typed_value
from .types import EvidenceIssue, Extraction, PROMPT_VERSION, SCHEMA_VERSION, SCOPES, SUBJECTS


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


class EvidenceCache:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def key(self, source, *, provider, model, schema=SCHEMA_VERSION, prompt=PROMPT_VERSION) -> str:
        # Same content in a different ledger, currency, or as-of context must not
        # inherit another request's binding or interpretation.
        data = {"content": source.content_hash, "schema": schema, "prompt": prompt, "provider": provider, "model": model,
                "source": source.source_id, "request": source.request_id, "user": source.user_id,
                "event": source.related_event_id, "currency": source.currency.value, "subject": source.subject,
                "direction": source.direction, "description": source.description, "observed_at": source.observed_at,
                "request_date": source.request_date, "settlement_date": source.settlement_date}
        return hashlib.sha256(canonical(data).encode()).hexdigest()

    def _path(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Invalid cache key")
        path = self.root / (key + ".json")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("Unsafe cache path")
        return path

    def put(self, key: str, source, extraction: Extraction):
        if not extraction.facts or any("usage_unknown" in record.operation for record in extraction.usage):
            return
        facts = []
        for fact in extraction.facts:
            # Only validated, bounded financial values and generated support refs.
            typed_value(fact.fact_type.value, None if fact.value is None else str(fact.value))
            facts.append({"kind": fact.fact_type.value, "value": None if fact.value is None else str(fact.value),
                          "currency": fact.currency.value if fact.currency else None, "subject": fact.subject,
                          "scope": fact.scope, "effective_date": str(fact.effective_date) if fact.effective_date else None,
                          "explicit": fact.explicit, "confidence": str(fact.confidence), "provider": fact.provenance.provider,
                          "rule": fact.notes[0], "support_ref": fact.support_ref})
        payload = {"schema": SCHEMA_VERSION, "source_hash": source.content_hash, "facts": facts,
                   "issues": [asdict(issue) for issue in extraction.issues],
                   "usage": [asdict(record) for record in extraction.usage], "latency_ms": extraction.latency_ms,
                   "policy": "generated-private-not-submission-approved"}
        envelope = {"key": key, "payload": payload, "checksum": hashlib.sha256(canonical(payload).encode()).hexdigest()}
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self._path(key)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp") as temp:
            temp.write(canonical(envelope))
            path = Path(temp.name)
        try:
            path.replace(destination)
        finally:
            path.unlink(missing_ok=True)

    def get(self, key: str, source) -> Extraction | None:
        try:
            path = self._path(key)
            if not path.exists() or path.stat().st_size > 500_000:
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope["payload"]
            if envelope["key"] != key or envelope["checksum"] != hashlib.sha256(canonical(payload).encode()).hexdigest() or payload["schema"] != SCHEMA_VERSION or payload["source_hash"] != source.content_hash:
                return None
            if len(payload["facts"]) > 64:
                return None
            facts = []
            from dataclasses import replace
            for item in payload["facts"]:
                if item["kind"] not in KINDS or item["subject"] not in SUBJECTS or item["scope"] not in SCOPES or type(item["explicit"]) is not bool:
                    return None
                for name in ("provider", "rule", "support_ref"):
                    if item[name] is not None and not re.fullmatch(r"[A-Za-z0-9_.,:+/\-]{1,220}", item[name]):
                        return None
                bound_source = replace(source, currency=Currency(item["currency"])) if item["currency"] else source
                facts.append(make_fact(bound_source, item["kind"], typed_value(item["kind"], item["value"]), subject=item["subject"], scope=item["scope"],
                                       effective_date=typed_value("effective_date", item["effective_date"]), explicit=item["explicit"], confidence=item["confidence"],
                                       provider=item["provider"], rule=item["rule"], support_ref=item["support_ref"]))
            # Historical token/cost records stay in the cache envelope, but a hit
            # never charges/reports the original model call again.
            return Extraction(tuple(facts), tuple(EvidenceIssue(**item) for item in payload["issues"]), latency_ms=payload["latency_ms"])
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, BuyWaitError):
            return None

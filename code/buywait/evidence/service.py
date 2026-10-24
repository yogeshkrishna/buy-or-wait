"""One request-scoped evidence boundary for CLI, ledger, API and evaluation."""
from __future__ import annotations
from collections import Counter
from dataclasses import replace
from decimal import Decimal
import hashlib
import re
import time
from buywait.domain.enums import EvidenceSourceKind
from buywait.domain.errors import BuyWaitError, UnresolvedEvidenceError
from .images import interpret_image
from .messages import interpret_message
from .resolver import resolve
from .routing import route
from .targeting import fact_applies_to_event
from .types import EvidenceIssue, EvidenceResult, Extraction, PROMPT_VERSION, SCHEMA_VERSION, MODEL_SCHEMA_VERSION


class EvidenceService:
    def __init__(self, context, *, ocr=None, model=None, cache=None, model_scope="mandatory_unresolved_images"):
        if model_scope not in {"mandatory_unresolved_images", "unrecognized", "all"}:
            raise ValueError("Unsupported evidence model scope.")
        self.context, self.ocr, self.model, self.cache = context, ocr, model, cache
        self.model_scope = model_scope
        self.documents = {}  # Private, in-memory raw OCR/bounds, never decision output.

    def extract(self, bundle):
        result = self._evaluate_bundle(bundle)
        self.require_ready(result)
        return result.facts

    @staticmethod
    def require_ready(result):
        if not result.ready:
            raise UnresolvedEvidenceError("Mandatory visual amounts are unresolved; install local OCR or explicitly enable a model provider.",
                                          context={"request_id": result.request_id, "event_ids": sorted(set(result.mandatory_event_ids) - set(result.resolved_event_ids))})

    def evaluate(self, request_id):
        bundle = self.context.bundle(request_id)
        return self._evaluate_bundle(bundle)

    def _evaluate_bundle(self, bundle):
        request_id = bundle.request.request_id
        sources, router_issues = route(bundle)
        all_facts, issues, usage, hits = [], list(router_issues), [], 0
        mandatory = tuple(sorted(event.event_id for event in bundle.events if event.amount is None))
        for source in sources:
            png, doc = None, None
            if source.kind == EvidenceSourceKind.IMAGE:
                try:
                    png = self.context.read_image(request_id, source.source_id)
                    source = replace(source, content_hash=hashlib.sha256(png).hexdigest())
                except BuyWaitError as error:
                    issues.append(EvidenceIssue(source.source_id, error.code, source.related_event_id))
                    continue
            provider_key = ":".join((self.ocr.provider_id, self.ocr.version)) if png is not None and self.ocr else "deterministic"
            model_key = ":".join((self.model.provider_id, self.model.model, self.model.version)) if self.model else "none"
            key = self.cache.key(source, provider=provider_key, model=model_key,
                                 schema=SCHEMA_VERSION + ":" + MODEL_SCHEMA_VERSION) if self.cache else None
            extraction = self.cache.get(key, source) if self.cache else None
            if extraction is not None:
                hits += 1
            else:
                started = time.monotonic()
                if png is None:
                    extraction = interpret_message(source)
                elif self.ocr is None:
                    extraction = Extraction(issues=(EvidenceIssue(source.source_id, "local_ocr_unavailable", source.related_event_id),))
                else:
                    try:
                        doc = self.ocr.recognize(png)
                        self.documents[(request_id, source.source_id)] = doc
                        extraction = self._interpret_ocr_ensemble(
                            source, png, doc, mandatory=source.related_event_id in mandatory
                        )
                    except (BuyWaitError, ValueError, TypeError) as error:
                        extraction = Extraction(issues=(EvidenceIssue(source.source_id, getattr(error, "code", "malformed_ocr"), source.related_event_id),))
                needs_model = (
                    self.model_scope == "all"
                    or self.model_scope == "unrecognized" and not extraction.facts
                    or self.model_scope == "mandatory_unresolved_images" and png is not None
                    and source.related_event_id in mandatory
                    and not any(fact.fact_type.value == "amount" for fact in extraction.facts)
                )
                if self.model and needs_model and not any(issue.reason_code == "embedded_instruction_quarantined" for issue in extraction.issues):
                    # Opt-in augmentation, not authority; disagreements reach resolver.
                    model_result = self.model.extract(source, image=png, ocr_text=doc.text if doc else "")
                    extraction = Extraction(extraction.facts + model_result.facts, extraction.issues + model_result.issues, model_result.usage)
                extraction = replace(extraction, latency_ms=round((time.monotonic() - started) * 1000))
                if self.cache:
                    self.cache.put(key, source, extraction)
            all_facts.extend(extraction.facts)
            issues.extend(extraction.issues)
            usage.extend(extraction.usage)
        facts, conflicts = resolve(all_facts, directions={event.event_id: event.direction for event in bundle.events})
        unresolved_conflict_ids = {fact_id for conflict in conflicts if conflict.selected_fact_id is None for fact_id in conflict.fact_ids}
        events_by_id = {event.event_id: event for event in bundle.events}
        resolved = tuple(sorted({
            fact.related_event_id for fact in facts
            if fact.related_event_id in mandatory
            and fact.fact_type.value == "amount"
            and isinstance(fact.value, Decimal)
            and fact.fact_id not in unresolved_conflict_ids
            and fact_applies_to_event(fact, events_by_id[fact.related_event_id])
        }))
        for event_id in set(mandatory) - set(resolved):
            linked = [source for source in sources if source.related_event_id == event_id]
            issues.append(EvidenceIssue(linked[0].source_id if linked else event_id, "mandatory_amount_unresolved", event_id, True))
        return EvidenceResult(request_id, facts, tuple(issues), conflicts, tuple(usage), mandatory, resolved, hits)

    def _interpret_ocr_ensemble(self, source, png, primary, *, mandatory: bool):
        """Prefer independently repeated OCR amounts; otherwise retain primary output."""
        primary_extraction = interpret_image(source, primary)
        if not mandatory:
            return primary_extraction
        if primary_extraction.facts and not re.search(r"\d[Ss]\d", primary.text):
            return primary_extraction
        variants = getattr(self.ocr, "variants", None)
        if variants is None:
            return primary_extraction
        documents = [primary]
        try:
            for document in variants(png):
                documents.append(document)
        except (BuyWaitError, ValueError, TypeError):
            return primary_extraction
        extractions = [primary_extraction]
        for document in documents:
            repaired_words = []
            changed = False
            for word in document.words:
                compact = re.sub(r"[^0-9Ss]", "", word.text)
                if re.fullmatch(r"\d+[Ss]\d+", compact):
                    repaired_words.append(replace(word, text=compact.replace("S", "5").replace("s", "5")))
                    changed = True
                else:
                    repaired_words.append(word)
            if changed:
                repaired = replace(document, words=tuple(repaired_words))
                extraction = interpret_image(source, repaired)
                if extraction.facts:
                    extractions.append(extraction)
        amounts = Counter(
            fact.value for extraction in extractions for fact in extraction.facts
            if fact.fact_type.value == "amount" and isinstance(fact.value, Decimal)
        )
        if not amounts:
            return primary_extraction
        value, count = min(amounts.items(), key=lambda item: (-item[1], item[0]))
        tied = sum(1 for candidate_count in amounts.values() if candidate_count == count) > 1
        if count < 2 or tied:
            return primary_extraction
        selected = next(
            extraction for extraction in extractions
            if any(fact.fact_type.value == "amount" and fact.value == value for fact in extraction.facts)
        )
        return selected

    def preflight(self, *, include_samples=True):
        requests = self.context.evaluation_requests + (self.context.sample_requests if include_samples else ())
        results = [self.evaluate(request.request_id) for request in requests]
        reasons = Counter(issue.reason_code for result in results for issue in result.issues)
        return {"schema_version": SCHEMA_VERSION, "requests": len(results), "ready_requests": sum(result.ready for result in results),
                "mandatory_events": len({event for result in results for event in result.mandatory_event_ids}),
                "resolved_events": len({event for result in results for event in result.resolved_event_ids}),
                "unresolved": [{"request_id": result.request_id, "event_ids": sorted(set(result.mandatory_event_ids) - set(result.resolved_event_ids))}
                               for result in results if not result.ready], "issue_counts": dict(sorted(reasons.items())),
                "fact_count": sum(len(result.facts) for result in results), "conflict_count": sum(len(result.conflicts) for result in results),
                "cache_hits": sum(result.cache_hits for result in results), "model_calls": sum(len(result.usage) for result in results),
                "input_tokens": sum(record.input_tokens for result in results for record in result.usage),
                "output_tokens": sum(record.output_tokens for result in results for record in result.usage),
                "unknown_usage_calls": sum("usage_unknown" in record.operation for result in results for record in result.usage)}

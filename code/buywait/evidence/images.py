"""Spatial financial-label extraction, not a maximum-number heuristic."""
from __future__ import annotations
import re
from datetime import datetime, timedelta
from decimal import Decimal
from buywait.domain.errors import UnresolvedEvidenceError
from .facts import make_fact
from .numbers import financial_number
from .safety import blocked_content, normalized_text
from .types import EvidenceIssue, EvidenceSource, Extraction, OcrDocument, OcrWord


def spatial_rows(document: OcrDocument) -> tuple[tuple[str, str], ...]:
    rows: list[list] = []
    for word in sorted(document.words, key=lambda w: (w.y + w.height / 2, w.x)):
        if min(word.x, word.y, word.width, word.height) < 0 or word.x + word.width > document.width + 2 or word.y + word.height > document.height + 2:
            raise UnresolvedEvidenceError("OCR bounds outside image.", code="malformed_ocr")
        center = word.y + word.height / 2
        row = next((row for row in reversed(rows[-4:]) if abs(center - row[0]) <= max(3, min(word.height, row[1]) * .45)), None)
        if row is None:
            rows.append([center, word.height, [word]])
        else:
            row[2].append(word)
    result = []
    for _, _, words in rows:
        words.sort(key=lambda w: w.x)
        result.append((normalized_text(" ".join(word.text for word in words)),
                       "bbox:" + ",".join(map(str, (min(w.x for w in words), min(w.y for w in words),
                                                      max(w.x + w.width for w in words), max(w.y + w.height for w in words))))))
    return tuple(result)


def _dated_due_amounts(source, document):
    """Read stacked due-date boxes without mixing a neighboring tax column."""
    facts = []
    found_labels = []
    for word in document.words:
        if "amount" not in word.text.lower():
            continue
        adjacent = sorted((w for w in document.words if w.x >= word.x and w.x < word.x + 300 and abs(w.y-word.y) < word.height / 2), key=lambda w: w.x)
        label_text = " ".join(w.text for w in adjacent)
        compact = re.sub(r"\s+", "", label_text).lower()
        if "amountdueafter" in compact or "amountduetill" in compact or "amountduebefore" in compact:
            found_labels.append(OcrWord(label_text, word.x, word.y, word.width, word.height))
    for label in found_labels:
        next_label_y = min((w.y for w in found_labels if w.y > label.y), default=document.height)
        nearby = [w for w in document.words if w.y >= label.y - label.height // 2 and
                  w.y < min(label.y + label.height * 5, next_label_y)]
        date_words = [(w, re.search(r"\b(\d{2})[- /]([A-Za-z]{3})[- /](\d{4})\b", w.text)) for w in nearby if abs(w.x-label.x) < 180]
        date_words = [(w, match) for w, match in date_words if match]
        if len(date_words) != 1:
            continue
        date_word, match = date_words[0]
        if not match:
            continue
        try:
            due = datetime.strptime("-".join(match.groups()), "%d-%b-%Y").date()
        except ValueError:
            continue
        numeric = []
        for word in nearby:
            if re.fullmatch(r"[₹$?]?[0-9Oo.,]+", word.text.strip()) and re.search(r"[.,]\d{2}$", word.text.strip()):
                try:
                    numeric.append((word, financial_number(word.text.strip().lstrip("₹$?"), ocr=True)))
                except UnresolvedEvidenceError:
                    pass
        aligned = [(w, value) for w, value in numeric if w.x > date_word.x + date_word.width and abs(w.y-date_word.y) < date_word.height / 2]
        if aligned:
            numeric = [min(aligned, key=lambda item: item[0].x)]
        else:
            numeric = [(w, value) for w, value in numeric if w.y > date_word.y and abs(w.x-label.x) < 180]
        if len(numeric) != 1:
            continue
        _, amount = numeric[0]
        after = "after" in label.text.replace(" ", "").lower()
        facts.append(make_fact(source, "amount", amount, provider=document.provider, rule="dated_due_box",
                               effective_date=due + timedelta(days=1) if after else None, explicit=after,
                               confidence="0.92", support_ref=f"bbox:{label.x},{label.y},{label.width},{label.height}"))
    return tuple(facts), bool(found_labels)


def interpret_image(source: EvidenceSource, document: OcrDocument) -> Extraction:
    rows = spatial_rows(document)
    if blocked_content(document.text) or blocked_content(" ".join(text for text, _ in rows)):
        return Extraction(issues=(EvidenceIssue(source.source_id, "embedded_instruction_quarantined", source.related_event_id),))
    dated, has_dated = _dated_due_amounts(source, document)
    if has_dated:
        if len(dated) >= 2 and any(f.effective_date for f in dated):
            return Extraction(dated)
        return Extraction(issues=(EvidenceIssue(source.source_id, "dated_bill_requires_review", source.related_event_id),))
    # Higher ranks are more specific payable/net labels. Totals of taxes/quantities
    # and available wallet balances are intentionally not payment amounts.
    labels = [(100, r"\bnet\s*pay\b"), (95, r"\bbalance\s*due\b"),
              (90, r"\b(?:net\s*amount|net\s*payable|amount\s*payable|grand\s*total)\b"),
              (85, r"\b(?:total\s*(?:bill\s*)?(?:amount|amoant)|total\s*fare|bill\s*amount|total\s*paid)\b"),
              (80, r"\bamount\s*due\b"), (70, r"\b(?:net\s*total|total|item\s*bill)\b")]
    candidates = []
    for text, bbox in rows:
        if re.search(r"total (?:tax|qty|quantity|earnings|deductions|savings)|(?:sub|round)\s*total|wallet balance", text, re.I):
            continue
        for rank, pattern in labels:
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            suffix = text[match.end():].strip(" :")
            if re.search(r"\b(?:items|tax|qty|quantity|units|deductions|earnings|discount)\b", suffix, re.I):
                break
            # No OCR repairs in general prose; only the isolated numeric column.
            # A numeric tail is mandatory; a corrupt suffix cannot expose an
            # unrelated earlier digit as the payable amount.
            tail = re.search(r"(?:[0-9OoIl|][0-9OoIl|., ]*[0-9OoIl|]|\d)\s*$", suffix)
            tokens = re.findall(r"(?<![\w/])(?:[0-9OoIl|][0-9OoIl|.,]*[0-9OoIl|]|\d)(?![\w/])", suffix)
            if tail and re.fullmatch(r"[0-9OoIl| ]+\.[0-9OoIl|]{1,2}", tail.group().strip()):
                try:
                    grouped = financial_number(tail.group().strip(), ocr=True)
                    tokens = [str(grouped)]
                except UnresolvedEvidenceError:
                    pass
            if not tokens or not tail:
                continue
            try:
                amount = financial_number(tokens[-1].rstrip(".,"), ocr=True)
            except UnresolvedEvidenceError:
                continue
            if amount >= 0:
                candidates.append((rank, amount, bbox))
            break
    if not candidates:
        return Extraction(issues=(EvidenceIssue(source.source_id, "image_amount_unrecognized", source.related_event_id),))
    best = [item for item in candidates if item[0] == max(row[0] for row in candidates)]
    if len({item[1] for item in best}) > 1:
        return Extraction(issues=(EvidenceIssue(source.source_id, "image_amount_ambiguous", source.related_event_id),))
    _, amount, bbox = best[0]
    return Extraction((make_fact(source, "amount", amount, provider=document.provider, rule="spatial_payable_label",
                                 confidence="0.90", support_ref=bbox),))

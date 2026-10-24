"""Bounded bilingual financial templates; unknown language never becomes a fact."""
from __future__ import annotations
from dataclasses import replace
from datetime import date
from decimal import Decimal
import re
from buywait.domain.enums import Currency
from buywait.domain.errors import UnresolvedEvidenceError
from .facts import make_fact
from .numbers import financial_number
from .safety import blocked_content, normalized_text
from .types import EvidenceIssue, EvidenceSource, Extraction

MONEY = re.compile(r"\b(INR|IDR|EUR|USD|ZAR)\s*([0-9][0-9.,]*)", re.I)


def interpret_message(source: EvidenceSource) -> Extraction:
    text = normalized_text(source.text)
    if blocked_content(text):
        return Extraction(issues=(EvidenceIssue(source.source_id, "embedded_instruction_quarantined", source.related_event_id),))
    lower = text.lower()
    facts, issues = [], []

    def has(pattern):
        return bool(re.search(pattern, lower))

    def add(kind, value, subject="event", scope="one_time", effective=None, explicit=False, rule="message_template"):
        fact = make_fact(source, kind, value, subject=subject, scope=scope, effective_date=effective, explicit=explicit, rule=rule)
        if fact.fact_id not in {item.fact_id for item in facts}:
            facts.append(fact)

    dates = []
    for token in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        try:
            dates.append(date.fromisoformat(token))
        except ValueError:
            issues.append(EvidenceIssue(source.source_id, "invalid_evidence_date", source.related_event_id))
    effective = dates[0] if len(set(dates)) == 1 else None
    if len(set(dates)) > 1:
        issues.append(EvidenceIssue(source.source_id, "multiple_dates_require_review", source.related_event_id))

    salary_negated = has(r"not (?:your )?regular salary|bukan (?:merupakan )?gaji (?:rutin|reguler)")
    salary = has(r"\bsalary\b|\bpayroll\b|\bpay\b|gaji|penggajian|seasonal contract|kontrak.*berakhir") and not salary_negated
    if salary:
        temporary = has(r"temporary|next salary|next payroll|next pay|affected pay cycle|unpaid leave|berikutnya.*(?:dikurangi|cuti)")
        scope = "next_cycle" if temporary else "recurring"
        confirmed = has(r"confirmed|confirmation|dik[o]?nfirmasi|naik menjadi|has changed|will be| is |resumes|gaji pokok")
        if confirmed:
            add("status", "confirmed", "salary", scope, effective)
        for match in MONEY.finditer(text):
            # Associate the amount with its own clause, not every amount in a message.
            prefix = lower[max(0, match.start() - 100):match.start()]
            clause = lower[max(0, match.start() - 100):min(len(lower), match.end() + 100)]
            if not re.search(r"salary|pay\b|gaji", clause) or re.search(r"(?:bonus|commission|arrears|tunggakan)[^.;]*$", prefix):
                continue
            try:
                value = financial_number(match.group(2).rstrip(".,"))
                fact = make_fact(replace(source, currency=Currency(match.group(1).upper())), "amount", value,
                                 subject="salary", scope=scope, effective_date=effective,
                                 explicit=has(r"changed|reduced|naik|replaces|resumes|temporary"), rule="salary_amount")
                facts.append(fact)
            except UnresolvedEvidenceError:
                issues.append(EvidenceIssue(source.source_id, "ambiguous_message_amount", source.related_event_id))
        if effective and has(r"expected on|credit on|credited on|salary date|payroll date|dikreditkan|tanggal|resumes|berlaku mulai"):
            add("settlement_date" if has(r"expected on|credit|payroll date|salary date|dikreditkan|tanggal") else "effective_date",
                effective, "salary", scope, effective, has(r"replaces|changed|updated|mengganti|berubah"))
        if has(r"contract.*ended|no .*income.*confirmed|no .*renewal.*confirmed|kontrak.*berakhir"):
            add("cancellation", "income_ended", "salary", "recurring", effective, True)
        if has(r"regular salary|gaji rutin"):
            add("recurrence", "regular", "salary", "recurring")

    if has(r"arrears|one.time adjustment|penyesuaian satu kali|tunggakan"):
        add("recurrence", "nonrecurring", "arrears")
        for match in MONEY.finditer(text):
            if re.search(r"arrears|adjustment|penyesuaian|tunggakan", lower[max(0, match.start()-70):match.start()]):
                if Currency(match.group(1).upper()) == source.currency:
                    add("amount", financial_number(match.group(2).rstrip(".,")), "arrears", effective=effective)
    for subject, noun in (("bonus", r"bonus"), ("commission", r"commission|komisi"), ("payout", r"payout|earnings"), ("prize", r"prize|lottery|hadiah")):
        if has(noun):
            pending = has(r"not.*(?:approved|confirmed|credited|withdrawable)|pending|processing|belum|can change|may change|unfinished")
            settled = has(r"(?:has|have).*reached|(?:has|have).*credited|settled|sudah.*(?:masuk|diterima)") and not pending
            add("cash_state", "settled" if settled else "pending_credit", subject, explicit=settled)
            if subject in {"bonus", "prize"} or has(r"one.time|satu kali|no further|claim is closed"):
                add("recurrence", "nonrecurring", subject)
    if has(r"refund|pengembalian dana"):
        pending = has(r"not.*(?:reached|credited|settled)|pending|initiated|belum")
        settled = has(r"settled|credited|reached|sudah.*(?:masuk|diterima)") and not pending
        add("cash_state", "settled" if settled else "pending_credit", "refund", explicit=settled)
        if settled:
            add("settlement", "settled", "refund", effective=effective, explicit=True)
    if has(r"transfer") and has(r"own accounts|your.*accounts|internal|rekening.*sendiri"):
        add("internal_transfer", "internal", "transfer", explicit=True)
    if has(r"failed|gagal") and has(r"still (?:due|outstanding|owed)|remains (?:due|outstanding)|must.*paid|retry|masih.*(?:tagihan|terutang)"):
        add("cash_state", "outstanding_debit", explicit=True)
    if has(r"cancelled|canceled|dibatalkan|terminated") and has(r"subscription|service|membership|langganan"):
        add("cancellation", "cancelled", "service", "recurring", effective, True)
    if has(r"investment|units|valuation|portfolio|investasi|unit|valuasi"):
        settled_sale = has(r"sale|sold|penjualan") and has(
            r"settled|credited|reached|masuk ke rekening|sudah.*masuk"
        )
        unrealized = has(
            r"no units (?:have been )?sold|units (?:have )?not been sold|holding has not been sold|"
            r"no cash (?:proceeds|transaction)|unrealized|displayed (?:market )?value|"
            r"belum.*(?:jual|dicairkan)|tidak ada transaksi tunai"
        )
        if settled_sale:
            add("settlement", "settled", "investment", effective=effective, explicit=True)
            add("recurrence", "nonrecurring", "investment")
        elif unrealized:
            add("cash_state", "unrealized", "investment")
    if has(r"rent|lease|sewa"):
        match = re.search(r"(?:increases?|raises?|naik).*?(\d+(?:\.\d+)?)\s*%", lower)
        if match:
            add("amendment", "increase_percent:" + str(Decimal(match.group(1))), "rent", "recurring", effective, True)
        if has(r"next.*payment|next.*rent|berikutnya"):
            add("effective_date", None, "rent", "next_cycle", explicit=True, rule="next_occurrence_requires_ledger_date")
    if has(r"childcare") and has(r"recurring|monthly|begins|starts"):
        add("recurrence", "regular", "childcare", "recurring", effective)
        issues.append(EvidenceIssue(source.source_id, "childcare_amount_requires_ledger", source.related_event_id))
    if has(r"payment|debit|bill|pembayaran|tagihan") and has(r"amended|rescheduled|new due date|delayed|ditunda|diubah"):
        if effective:
            add("settlement_date", effective, effective=effective, explicit=True)
        for match in MONEY.finditer(text):
            if Currency(match.group(1).upper()) == source.currency:
                add("amount", financial_number(match.group(2).rstrip(".,")), effective=effective, explicit=True)
    if not facts and not issues:
        issues.append(EvidenceIssue(source.source_id, "message_template_unrecognized", source.related_event_id))
    return Extraction(tuple(facts), tuple(issues))

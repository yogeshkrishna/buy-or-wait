"""Semantic binding of extracted facts to structured financial-event rows."""

from buywait.domain.enums import EventType
from buywait.domain.models import EvidenceFact, RawEvent


def fact_applies_to_event(fact: EvidenceFact, event: RawEvent) -> bool:
    """Return whether a fact's subject can safely mutate the linked event.

    A message can directly link to a receipt while also mentioning an unrelated
    salary. Link provenance is retained, but only the receipt clause may fill
    the receipt amount. The detached salary clause remains available to the
    recurrence stage.
    """
    subject = fact.subject.lower()
    if subject == "event":
        return True
    searchable = " ".join((event.category, event.description, event.event_type.value)).lower()
    if subject == "salary":
        return event.event_type is EventType.INCOME and (
            event.category == "salary" or "salary" in searchable or "payroll" in searchable
        )
    if subject == "refund":
        return event.event_type is EventType.REFUND
    if subject == "investment":
        return event.event_type in {
            EventType.INVESTMENT_PURCHASE,
            EventType.INVESTMENT_VALUATION,
            EventType.INVESTMENT_SALE,
        }
    if subject == "service":
        return event.event_type is EventType.SUBSCRIPTION or "service" in searchable
    if subject == "transfer":
        return "transfer" in searchable
    return subject in searchable

"""Read-only dataset indexes and request-scoped lazy media access."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from buywait.domain.enums import AffordabilityStatus, Direction, PaymentMethod
from buywait.domain.errors import InvalidInputError
from buywait.domain.fx import RateBook, RateKey
from buywait.domain.models import (
    EvaluationBundle, ExchangeRate, FinancialRequest, ImageRecord, MessageRecord,
    Payment, PaymentOption, Profile, RawEvent, SpendingChange,
)
from buywait.domain.money import Money

from .diagnostics import SourceLocation, ValidationIssue

T = TypeVar("T")


def grouped(items: Iterable[T], attribute: str) -> Mapping[str, tuple[T, ...]]:
    result: dict[str, list[T]] = defaultdict(list)
    for item in items:
        key = getattr(item, attribute)
        if key is not None:
            result[key].append(item)
    return MappingProxyType({key: tuple(value) for key, value in result.items()})


def resolve_dataset_dir(dataset_dir: str | Path | None = None, *, repository_dir: Path | None = None) -> Path:
    code_dir = Path(__file__).resolve().parents[3]
    root = repository_dir or (code_dir.parent if code_dir.name == "code" else code_dir)
    candidate = Path(dataset_dir) if dataset_dir is not None else Path("dataset")
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def image_path(dataset_dir: Path, image_id: str) -> Path:
    from .parsing import IDENTIFIER

    if not IDENTIFIER.fullmatch(image_id):
        raise InvalidInputError("Invalid image identifier.")
    directory = dataset_dir / "media" / "images"
    candidate = (directory / f"{image_id}.png").resolve()
    if not directory.resolve().is_relative_to(dataset_dir.resolve()) or not candidate.is_relative_to(directory.resolve()):
        raise InvalidInputError("Image reference escapes the dataset media directory.")
    return candidate


@dataclass(frozen=True, slots=True)
class SampleExpectation:
    """Public evaluation target, deliberately absent from EvaluationBundle."""

    request_id: str
    amount_safe_to_pay: Money
    affordability_status: AffordabilityStatus
    recommended_payment_method: PaymentMethod
    payment_plan: tuple[Payment, ...]
    earliest_date_for_full_payment: date | None
    spending_changes_needed: tuple[SpendingChange, ...]
    decision_explanation: str


@dataclass(frozen=True, slots=True)
class DatasetContext:
    dataset_dir: Path
    profiles: tuple[Profile, ...]
    evaluation_requests: tuple[FinancialRequest, ...]
    sample_requests: tuple[FinancialRequest, ...]
    events: tuple[RawEvent, ...]
    payment_options: tuple[PaymentOption, ...]
    messages: tuple[MessageRecord, ...]
    images: tuple[ImageRecord, ...]
    exchange_rates: tuple[ExchangeRate, ...]
    sample_expectations: Mapping[str, SampleExpectation]
    output_template: Mapping[str, SampleExpectation | None]
    source_locations: Mapping[tuple[str, str], SourceLocation]
    warnings: tuple[ValidationIssue, ...] = ()
    profiles_by_id: Mapping[str, Profile] = field(init=False, repr=False)
    requests_by_id: Mapping[str, FinancialRequest] = field(init=False, repr=False)
    events_by_id: Mapping[str, RawEvent] = field(init=False, repr=False)
    options_by_id: Mapping[str, PaymentOption] = field(init=False, repr=False)
    messages_by_id: Mapping[str, MessageRecord] = field(init=False, repr=False)
    images_by_id: Mapping[str, ImageRecord] = field(init=False, repr=False)
    requests_by_user_id: Mapping[str, tuple[FinancialRequest, ...]] = field(init=False, repr=False)
    events_by_user_id: Mapping[str, tuple[RawEvent, ...]] = field(init=False, repr=False)
    options_by_request_id: Mapping[str, tuple[PaymentOption, ...]] = field(init=False, repr=False)
    messages_by_user_id: Mapping[str, tuple[MessageRecord, ...]] = field(init=False, repr=False)
    messages_by_request_id: Mapping[str, tuple[MessageRecord, ...]] = field(init=False, repr=False)
    messages_by_event_id: Mapping[str, tuple[MessageRecord, ...]] = field(init=False, repr=False)
    images_by_user_id: Mapping[str, tuple[ImageRecord, ...]] = field(init=False, repr=False)
    images_by_request_id: Mapping[str, tuple[ImageRecord, ...]] = field(init=False, repr=False)
    images_by_event_id: Mapping[str, tuple[ImageRecord, ...]] = field(init=False, repr=False)
    rates_by_key: Mapping[RateKey, ExchangeRate] = field(init=False, repr=False)
    fx: RateBook = field(init=False, repr=False)

    def __post_init__(self) -> None:
        all_requests = self.evaluation_requests + self.sample_requests
        indexes = {
            "profiles_by_id": {item.user_id: item for item in self.profiles},
            "requests_by_id": {item.request_id: item for item in all_requests},
            "events_by_id": {item.event_id: item for item in self.events},
            "options_by_id": {item.payment_option_id: item for item in self.payment_options},
            "messages_by_id": {item.message_id: item for item in self.messages},
            "images_by_id": {item.image_id: item for item in self.images},
        }
        for name, index in indexes.items():
            object.__setattr__(self, name, MappingProxyType(index))
        groups = (
            ("requests_by_user_id", all_requests, "user_id"),
            ("events_by_user_id", self.events, "user_id"),
            ("options_by_request_id", self.payment_options, "request_id"),
            ("messages_by_user_id", self.messages, "user_id"),
            ("messages_by_request_id", self.messages, "request_id"),
            ("messages_by_event_id", self.messages, "related_event_id"),
            ("images_by_user_id", self.images, "user_id"),
            ("images_by_request_id", self.images, "request_id"),
            ("images_by_event_id", self.images, "related_event_id"),
        )
        for name, items, attribute in groups:
            object.__setattr__(self, name, grouped(items, attribute))
        book = RateBook(self.exchange_rates)
        object.__setattr__(self, "fx", book)
        object.__setattr__(self, "rates_by_key", book.rates)
        for name in ("sample_expectations", "output_template", "source_locations"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def bundle(self, request_id: str) -> EvaluationBundle:
        try:
            request = self.requests_by_id[request_id]
        except KeyError as error:
            raise InvalidInputError("Request ID is absent from the loaded dataset.") from error
        user_id = request.user_id
        events = self.events_by_user_id.get(user_id, ())
        event_ids = {item.event_id for item in events}

        def relevant(item: MessageRecord | ImageRecord) -> bool:
            return item.request_id in {None, request_id} or item.related_event_id in event_ids

        rates = tuple(rate for rate in self.exchange_rates if rate.to_currency == request.requested_amount.currency)
        return EvaluationBundle(
            request=request, profile=self.profiles_by_id[user_id], events=events,
            payment_options=self.options_by_request_id.get(request_id, ()),
            messages=tuple(item for item in self.messages_by_user_id.get(user_id, ()) if relevant(item)),
            images=tuple(item for item in self.images_by_user_id.get(user_id, ()) if relevant(item)),
            exchange_rates=rates,
        )

    def read_image(self, request_id: str, image_id: str, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        if max_bytes <= 0:
            raise InvalidInputError("Image byte limit must be positive.")
        if image_id not in {item.image_id for item in self.bundle(request_id).images}:
            raise InvalidInputError("Image is not linked to this request, user, or relevant event.")
        path = image_path(self.dataset_dir, image_id)
        try:
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as error:
            raise InvalidInputError("Linked image cannot be read.") from error
        if len(data) > max_bytes:
            raise InvalidInputError("Linked image exceeds the configured byte limit.")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise InvalidInputError("Linked image is not a PNG file.")
        return data

    def summary(self) -> dict[str, Any]:
        """Counts only: no row text, balances, request identifiers, or image contents."""
        foreign = [event for event in self.events if event.direction is not Direction.NON_CASH
                   and event.currency != self.profiles_by_id[event.user_id].home_currency]
        return {
            "schema_version": "buywait.dataset-summary.v1",
            "valid": True,
            "counts": {
                "evaluation_requests": len(self.evaluation_requests), "sample_requests": len(self.sample_requests),
                "profiles": len(self.profiles), "financial_events": len(self.events),
                "payment_options": len(self.payment_options), "messages": len(self.messages),
                "images": len(self.images), "exchange_rates": len(self.exchange_rates),
                "output_template_rows": len(self.output_template),
                "blank_event_amounts": sum(item.amount is None for item in self.events),
                "foreign_cash_events": len(foreign),
                "required_fx_keys": len({(event.settlement_date, event.currency,
                                         self.profiles_by_id[event.user_id].home_currency) for event in foreign}),
                "lifecycle_links": sum(item.linked_event_id is not None for item in self.events),
            },
            "home_currencies": dict(sorted(Counter(item.home_currency.value for item in self.profiles).items())),
            "option_counts_per_request": dict(sorted(Counter(
                str(len(self.options_by_request_id.get(item.request_id, ())))
                for item in self.evaluation_requests + self.sample_requests
            ).items())),
            "warnings": dict(sorted(Counter(item.code for item in self.warnings).items())),
        }

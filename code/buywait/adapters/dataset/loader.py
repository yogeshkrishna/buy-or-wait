"""Load only participant CSVs, validate joins, then publish a complete context."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from decimal import localcontext
from pathlib import Path
from typing import TypeVar

from buywait.domain.enums import (
    AffordabilityStatus, Currency, Direction, EventStatus, EventType, Flexibility,
    MessageSource, PaymentMethod, RequestType, SpendingChangeAction,
)
from buywait.domain.errors import BuyWaitError, MissingExchangeRateError
from buywait.domain.models import (
    ExchangeRate, FinancialRequest, ImageRecord, MessageRecord, Payment, PaymentOption,
    Profile, RawEvent, SpendingChange,
)
from buywait.domain.serialization import OUTPUT_COLUMNS

from .context import DatasetContext, SampleExpectation, image_path, resolve_dataset_dir
from .diagnostics import DatasetValidationError, SourceLocation, ValidationIssue
from .parsing import CellError, CsvRow, read_csv
from .schema import HEADERS

T = TypeVar("T")


class _Loader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.issues: list[ValidationIssue] = []
        self.rows: dict[str, tuple[CsvRow, ...]] = {}
        self.sources: dict[tuple[str, str], SourceLocation] = {}

    def checkpoint(self) -> None:
        if self.issues:
            raise DatasetValidationError(tuple(self.issues))

    def read(self) -> None:
        for filename, columns in HEADERS.items():
            path = self.root / filename
            try:
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError
            except (OSError, ValueError):
                self.issues.append(ValidationIssue("invalid_file_path", "CSV must resolve inside the supplied dataset directory.",
                                                   SourceLocation(filename, 0, 1)))
                continue
            rows, issues = read_csv(path, columns)
            self.rows[filename] = rows
            self.issues.extend(issues)
            if filename == "exchange_rates.csv":
                continue  # Its unique key is a parsed (date, from, to) tuple.
            seen: set[str] = set()
            for row in rows:
                try:
                    key = row.identifier(columns[0])
                except CellError as error:
                    self.issues.append(ValidationIssue(error.code, error.message, row.source, error.field))
                    continue
                if key in seen:
                    self.issues.append(ValidationIssue("duplicate_id", "Identifier must be unique in this file.", row.source, columns[0]))
                seen.add(key)
                self.sources[(filename, key)] = row.source
        self.checkpoint()

    def parse(self, filename: str, factory: Callable[[CsvRow], T]) -> tuple[T, ...]:
        result: list[T] = []
        for row in self.rows[filename]:
            try:
                result.append(factory(row))
            except CellError as error:
                self.issues.append(ValidationIssue(error.code, error.message, row.source, error.field))
            except BuyWaitError as error:
                # Domain context can contain financial amounts; do not echo it in diagnostics.
                self.issues.append(ValidationIssue(error.code, error.message, row.source, "record"))
        return tuple(result)

    @staticmethod
    def reference(row: CsvRow, field: str, index: Mapping[str, T]) -> T:
        key = row.identifier(field)
        try:
            return index[key]
        except KeyError as error:
            raise CellError(field, "Referenced identifier does not exist in its parent file.", "missing_link") from error

    def profile(self, row: CsvRow) -> Profile:
        currency = row.enum("home_currency", Currency)
        methods = []
        for value in row.items("payment_methods_user_will_consider", optional=False):
            try:
                methods.append(PaymentMethod(value))
            except ValueError as error:
                raise CellError("payment_methods_user_will_consider", "Unknown considered payment method.") from error
        return Profile(
            user_id=row.identifier("user_id"), home_currency=currency,
            current_available_balance=row.money("current_available_balance", currency),
            minimum_balance_to_keep=row.money("minimum_balance_to_keep", currency),
            financial_priorities=row.items("financial_priorities"),
            protected_categories=frozenset(row.items("expense_categories_to_protect")),
            reducible_categories=frozenset(row.items("expense_categories_user_is_willing_to_reduce")),
            stoppable_categories=frozenset(row.items("expense_categories_user_is_willing_to_stop")),
            payment_methods_user_will_consider=frozenset(methods),
            max_installment_months=row.integer("max_installment_months", optional=True),
        )

    def request(self, row: CsvRow, profiles: Mapping[str, Profile]) -> FinancialRequest:
        profile = self.reference(row, "user_id", profiles)
        return FinancialRequest(
            request_id=row.identifier("request_id"), user_id=profile.user_id,
            request_date=row.day("request_date"), request_type=row.enum("request_type", RequestType),
            requested_amount=row.money("requested_amount", profile.home_currency),
            desired_completion_date=row.day("desired_completion_date"),
            allows_partial_payment=row.boolean("allows_partial_payment"), request_text=row.text("request_text"),
        )

    def event(self, row: CsvRow, profiles: Mapping[str, Profile]) -> RawEvent:
        profile = self.reference(row, "user_id", profiles)
        currency = row.enum("currency", Currency)
        return RawEvent(
            event_id=row.identifier("event_id"), user_id=profile.user_id,
            event_type=row.enum("event_type", EventType), description=row.text("description"),
            category=row.text("category"), direction=row.enum("direction", Direction),
            amount=row.money("amount", currency, optional=True), currency=currency,
            event_date=row.day("event_date"), settlement_date=row.day("settlement_date", optional=True),
            status=row.enum("status", EventStatus), linked_event_id=row.identifier("linked_event_id", optional=True),
            flexibility=row.enum("flexibility", Flexibility),
            minimum_allowed_amount=row.money("minimum_allowed_amount", currency, optional=True),
        )

    def option(self, row: CsvRow, requests: Mapping[str, FinancialRequest]) -> PaymentOption:
        request = self.reference(row, "request_id", requests)
        currency = request.requested_amount.currency
        return PaymentOption(
            payment_option_id=row.identifier("payment_option_id"), request_id=request.request_id,
            payment_method=row.enum("payment_method", PaymentMethod),
            payment_amount=row.money("payment_amount", currency),
            number_of_payments=row.integer("number_of_payments"),
            first_payment_date=row.day("first_payment_date"),
            payment_frequency_days=row.integer("payment_frequency_days", optional=True),
            financing_fee=row.money("financing_fee", currency),
            total_payable_amount=row.money("total_payable_amount", currency),
        )

    def expectation(self, row: CsvRow, requests: Mapping[str, FinancialRequest]) -> SampleExpectation | None:
        request = self.reference(row, "request_id", requests)
        if row.source.filename == "output.csv" and all(not row.text(key, optional=True) for key in OUTPUT_COLUMNS[1:]):
            return None
        currency = request.requested_amount.currency
        amount = row.money("amount_safe_to_pay", currency)
        if amount.amount > request.requested_amount.amount:
            raise CellError("amount_safe_to_pay", "Safe amount exceeds requested amount.")
        payments: list[Payment] = []
        changes: list[SpendingChange] = []
        plan = row.text("payment_plan")
        if plan != "none":
            for sequence, component in enumerate(plan.split("|"), start=1):
                parts = component.split(":")
                if len(parts) != 2:
                    raise CellError("payment_plan", "Expected date:amount payments separated by pipes.")
                cell = CsvRow(row.source, {"date": parts[0], "amount": parts[1]})
                try:
                    payments.append(Payment(cell.day("date"), cell.money("amount", currency), sequence))
                except CellError as error:
                    raise CellError("payment_plan", error.message) from error
            if payments != sorted(payments, key=lambda item: item.payment_date):
                raise CellError("payment_plan", "Payments must be in chronological order.")
        change_text = row.text("spending_changes_needed")
        if change_text != "none":
            for component in change_text.split("|"):
                parts = component.split(":")
                if len(parts) == 2 and parts[0] == "stop":
                    cell = CsvRow(row.source, {"id": parts[1]})
                    changes.append(SpendingChange(SpendingChangeAction.STOP, cell.identifier("id")))
                elif len(parts) == 3 and parts[0] == "reduce_to":
                    cell = CsvRow(row.source, {"id": parts[1], "amount": parts[2]})
                    changes.append(SpendingChange(SpendingChangeAction.REDUCE_TO,
                                                  cell.identifier("id"), cell.money("amount", currency)))
                else:
                    raise CellError("spending_changes_needed", "Expected stop:id or reduce_to:id:amount.")
            if len(changes) > 3 or len({item.event_id for item in changes}) != len(changes):
                raise CellError("spending_changes_needed", "At most three distinct events may be changed.")
        return SampleExpectation(
            request.request_id, amount, row.enum("affordability_status", AffordabilityStatus),
            row.enum("recommended_payment_method", PaymentMethod), tuple(payments),
            row.day("earliest_date_for_full_payment", optional=True), tuple(changes), row.text("decision_explanation"),
        )

    def load(self) -> DatasetContext:
        self.read()
        profiles = self.parse("financial_profiles.csv", self.profile)
        self.checkpoint()
        profiles_by_id = {item.user_id: item for item in profiles}
        requests = self.parse("requests.csv", lambda row: self.request(row, profiles_by_id))
        samples = self.parse("sample_requests.csv", lambda row: self.request(row, profiles_by_id))
        request_index = {item.request_id: item for item in requests + samples}
        if len(request_index) != len(requests) + len(samples):
            for item in samples:
                if item.request_id in {request.request_id for request in requests}:
                    self.issues.append(ValidationIssue("duplicate_id", "Request IDs must be unique across evaluation and sample files.",
                                                       self.sources[("sample_requests.csv", item.request_id)], "request_id"))
        self.checkpoint()
        events = self.parse("financial_events.csv", lambda row: self.event(row, profiles_by_id))
        options = self.parse("request_payment_options.csv", lambda row: self.option(row, request_index))
        rates = self.parse("exchange_rates.csv", lambda row: ExchangeRate(
            row.day("rate_date"), row.enum("from_currency", Currency), row.enum("to_currency", Currency), row.number("rate"),
        ))
        messages = self.parse("messages.csv", lambda row: MessageRecord(
            row.identifier("message_id"), self.reference(row, "user_id", profiles_by_id).user_id,
            row.timestamp("sent_at"), row.enum("source_type", MessageSource), row.text("message_text"),
            row.identifier("request_id", optional=True), row.identifier("related_event_id", optional=True),
        ))
        images = self.parse("images.csv", lambda row: ImageRecord(
            row.identifier("image_id"), self.reference(row, "user_id", profiles_by_id).user_id,
            row.identifier("image_id"), row.identifier("request_id", optional=True),
            row.identifier("related_event_id", optional=True),
        ))
        expectations = self.parse("sample_requests.csv", lambda row: self.expectation(row, request_index))
        evaluation_index = {item.request_id: item for item in requests}
        output_values = self.parse("output.csv", lambda row: self.expectation(row, evaluation_index))
        self.checkpoint()
        seen_rates = set()
        for row, rate in zip(self.rows["exchange_rates.csv"], rates):
            key = rate.rate_date, rate.from_currency, rate.to_currency
            text_key = (rate.rate_date.isoformat(), rate.from_currency.value, rate.to_currency.value)
            if key in seen_rates:
                self.issues.append(ValidationIssue("duplicate_rate_key", "Dated currency-pair key must be unique.", row.source, key=text_key))
            seen_rates.add(key)
            self.sources[("exchange_rates.csv", "|".join(text_key))] = row.source
        self.checkpoint()
        output = {row.identifier("request_id"): value for row, value in zip(self.rows["output.csv"], output_values)}
        context = DatasetContext(
            self.root, profiles, requests, samples, events, options, messages, images, rates,
            {item.request_id: item for item in expectations}, output, self.sources,
        )
        issues, warnings = validate_context(context)
        self.issues.extend(issues)
        self.checkpoint()
        return replace(context, warnings=warnings)


def validate_context(context: DatasetContext) -> tuple[tuple[ValidationIssue, ...], tuple[ValidationIssue, ...]]:
    issues: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    def report(code: str, message: str, filename: str, record_id: str, field: str = "", *,
               warning: bool = False, key: tuple[str, ...] = ()) -> None:
        issue = ValidationIssue(code, message,
                                context.source_locations.get((filename, record_id), SourceLocation(filename, 0, 1, record_id)),
                                field, "warning" if warning else "error", key)
        (warnings if warning else issues).append(issue)

    evaluation_ids = {item.request_id for item in context.evaluation_requests}
    if set(context.output_template) != evaluation_ids:
        for request_id in sorted(evaluation_ids - set(context.output_template)):
            report("missing_template_request", "Evaluation request is missing from the output template.", "output.csv", request_id, "request_id")

    for event in context.events:
        if event.linked_event_id is not None:
            parent = context.events_by_id.get(event.linked_event_id)
            if parent is None or parent.user_id != event.user_id:
                report("invalid_lifecycle_link", "Linked event must exist and belong to the same user.",
                       "financial_events.csv", event.event_id, "linked_event_id")
        if event.amount is None:
            if not context.images_by_event_id.get(event.event_id):
                report("missing_amount_evidence", "Blank event amount requires a directly linked image.",
                       "financial_events.csv", event.event_id, "amount")
            else:
                report("image_amount_unresolved", "Image-linked amount awaits Phase 04 extraction.",
                       "financial_events.csv", event.event_id, "amount", warning=True)
        home_currency = context.profiles_by_id[event.user_id].home_currency
        if event.direction is not Direction.NON_CASH and event.currency != home_currency:
            if event.settlement_date is None:
                report("missing_fx_date", "Foreign cash event requires its settlement date for FX lookup.",
                       "financial_events.csv", event.event_id, "settlement_date",
                       key=("<missing settlement_date>", event.currency.value, home_currency.value))
            else:
                try:
                    context.fx.lookup(event.settlement_date, event.currency, home_currency)
                except MissingExchangeRateError:
                    report("missing_exchange_rate", "No supplied rate for this exact settlement date and direction.",
                           "financial_events.csv", event.event_id, "currency",
                           key=(event.settlement_date.isoformat(), event.currency.value, home_currency.value))

    # Linear-time link-cycle check; never recurse through a potentially long chain.
    completed: set[str] = set()
    for event in context.events:
        chain: set[str] = set()
        current: str | None = event.event_id
        while current in context.events_by_id and current not in completed:
            if current in chain:
                report("lifecycle_cycle", "Lifecycle links form a cycle.", "financial_events.csv", current, "linked_event_id")
                break
            chain.add(current)
            current = context.events_by_id[current].linked_event_id
        completed.update(chain)

    for filename, records, id_attribute in (
        ("messages.csv", context.messages, "message_id"), ("images.csv", context.images, "image_id"),
    ):
        for record in records:
            record_id = getattr(record, id_attribute)
            for field, index, reference in (
                ("request_id", context.requests_by_id, record.request_id),
                ("related_event_id", context.events_by_id, record.related_event_id),
            ):
                if reference is not None:
                    target = index.get(reference)
                    if target is None or target.user_id != record.user_id:
                        report("invalid_evidence_link", "Evidence link must exist and belong to the same user.", filename, record_id, field)

    for image in context.images:
        try:
            if not image_path(context.dataset_dir, image.image_id).is_file():
                report("missing_image", "Expected media/images/<image_id>.png is missing.", "images.csv", image.image_id, "image_id")
        except (BuyWaitError, OSError):
            report("invalid_image_path", "Image must resolve inside the dataset media directory.", "images.csv", image.image_id, "image_id")

    for option in context.payment_options:
        request = context.requests_by_id[option.request_id]
        filename, record_id = "request_payment_options.csv", option.payment_option_id
        with localcontext() as arithmetic:
            arithmetic.prec = 220  # Parsed decimals have at most 100 characters each.
            principal_and_fee = request.requested_amount.amount + option.financing_fee.amount
            total = option.total_payable_amount.amount
            repeated = option.payment_amount.amount * option.number_of_payments
            if total != principal_and_fee:
                report("invalid_option_total", "Total payable must equal requested amount plus financing fee.", filename, record_id, "total_payable_amount")
            if option.payment_amount.amount <= 0:
                report("invalid_option_payment", "Payment amount must be positive.", filename, record_id, "payment_amount")
            if repeated != total:
                report("invalid_option_schedule", "Payment count multiplied by payment amount must equal the stated total exactly.", filename, record_id, "payment_amount")
        if option.first_payment_date < request.request_date:
            report("option_before_request", "An offered payment cannot begin before the request date.", filename, record_id, "first_payment_date")
        try:
            last = option.first_payment_date + timedelta(days=(option.number_of_payments - 1) * (option.payment_frequency_days or 0))
            if last > request.desired_completion_date:
                report("option_after_deadline", "Offer completes after the request deadline; planner must reject it.", filename, record_id, warning=True)
        except OverflowError:
            report("invalid_option_date_range", "Offered schedule exceeds the supported date range.", filename, record_id)

    for request in context.evaluation_requests + context.sample_requests:
        if not context.options_by_request_id.get(request.request_id):
            source = "requests.csv" if request.request_id in evaluation_ids else "sample_requests.csv"
            report("missing_payment_options", "Request has no supplied payment option.", source, request.request_id)
    for filename, targets in (("sample_requests.csv", context.sample_expectations), ("output.csv", context.output_template)):
        for request_id, target in targets.items():
            if target is None:
                continue
            for change in target.spending_changes_needed:
                event = context.events_by_id.get(change.event_id)
                if event is None or event.user_id != context.requests_by_id[request_id].user_id:
                    report("invalid_change_link", "Output change must reference an event belonging to the requesting user.", filename, request_id, "spending_changes_needed")
    return tuple(issues), tuple(warnings)


def load_dataset(dataset_dir: str | Path | None = None, *, repository_dir: Path | None = None) -> DatasetContext:
    """Return a complete validated context or raise DatasetValidationError; never partial success."""
    try:
        root = resolve_dataset_dir(dataset_dir, repository_dir=repository_dir)
    except (OSError, ValueError) as error:
        raise DatasetValidationError((ValidationIssue("invalid_dataset_path", "Dataset directory cannot be resolved.",
                                                       SourceLocation("dataset", 0, 1)),)) from error
    return _Loader(root).load()

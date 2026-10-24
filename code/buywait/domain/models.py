"""Immutable domain records shared by ingestion, forecasting, and planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TypeAlias

from .enums import (
    AffordabilityStatus,
    AmountStrategy,
    CashState,
    CandidateRejectionCode,
    Currency,
    Direction,
    EventStatus,
    EventType,
    EvidenceFactType,
    EvidenceSourceKind,
    Flexibility,
    ForecastCheckpointKind,
    ForecastOrigin,
    LifecycleDisposition,
    MessageSource,
    PaymentMethod,
    RecurrenceCadence,
    RequestType,
    SpendingChangeAction,
    ViolationCode,
)
from .errors import InvalidInputError, InvariantViolationError
from .money import Money, parse_decimal


EvidenceValue: TypeAlias = str | Decimal | date | bool | None


def _required_identifier(name: str, value: str) -> None:
    if not value or not value.strip():
        raise InvalidInputError(f"{name} must be nonblank.")


def _nonnegative(name: str, money: Money) -> None:
    if money.amount < 0:
        raise InvalidInputError(
            f"{name} must be nonnegative.", context={"amount": str(money.amount)}
        )


def _same_currency(name: str, values: tuple[Money, ...]) -> Currency:
    currencies = {value.currency for value in values}
    if len(currencies) != 1:
        raise InvalidInputError(
            f"{name} must use one currency.",
            context={"currencies": sorted(currency.value for currency in currencies)},
        )
    return next(iter(currencies))


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise InvalidInputError("Date range end cannot precede start.")

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end


@dataclass(frozen=True, slots=True)
class Profile:
    user_id: str
    home_currency: Currency
    current_available_balance: Money
    minimum_balance_to_keep: Money
    financial_priorities: tuple[str, ...] = ()
    protected_categories: frozenset[str] = field(default_factory=frozenset)
    reducible_categories: frozenset[str] = field(default_factory=frozenset)
    stoppable_categories: frozenset[str] = field(default_factory=frozenset)
    payment_methods_user_will_consider: frozenset[PaymentMethod] = field(default_factory=frozenset)
    max_installment_months: int | None = None

    def __post_init__(self) -> None:
        _required_identifier("user_id", self.user_id)
        if self.current_available_balance.currency != self.home_currency:
            raise InvalidInputError("Current balance must use the home currency.")
        if self.minimum_balance_to_keep.currency != self.home_currency:
            raise InvalidInputError("Minimum balance must use the home currency.")
        _nonnegative("current_available_balance", self.current_available_balance)
        _nonnegative("minimum_balance_to_keep", self.minimum_balance_to_keep)
        if self.max_installment_months is not None and self.max_installment_months <= 0:
            raise InvalidInputError("max_installment_months must be positive when supplied.")
        immediate_methods = {
            PaymentMethod.FULL_PAYMENT,
            PaymentMethod.PARTIAL_PAYMENT,
            PaymentMethod.INSTALLMENTS,
        }
        invalid = self.payment_methods_user_will_consider - immediate_methods
        if invalid:
            raise InvalidInputError(
                "Profile contains an invalid considered payment method.",
                context={"methods": sorted(method.value for method in invalid)},
            )


@dataclass(frozen=True, slots=True)
class FinancialRequest:
    request_id: str
    user_id: str
    request_date: date
    request_type: RequestType
    requested_amount: Money
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str

    def __post_init__(self) -> None:
        _required_identifier("request_id", self.request_id)
        _required_identifier("user_id", self.user_id)
        _nonnegative("requested_amount", self.requested_amount)
        if self.desired_completion_date < self.request_date:
            raise InvalidInputError("Desired completion date cannot precede request date.")


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """Raw message metadata; text remains untrusted evidence until extracted."""

    message_id: str
    user_id: str
    sent_at: datetime
    source_type: MessageSource
    message_text: str
    request_id: str | None = None
    related_event_id: str | None = None

    def __post_init__(self) -> None:
        _required_identifier("message_id", self.message_id)
        _required_identifier("user_id", self.user_id)


@dataclass(frozen=True, slots=True)
class ImageRecord:
    """Image linkage with an opaque content reference, never a domain filesystem path."""

    image_id: str
    user_id: str
    content_ref: str
    request_id: str | None = None
    related_event_id: str | None = None

    def __post_init__(self) -> None:
        _required_identifier("image_id", self.image_id)
        _required_identifier("user_id", self.user_id)
        _required_identifier("content_ref", self.content_ref)


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    rate_date: date
    from_currency: Currency
    to_currency: Currency
    rate: Decimal

    def __post_init__(self) -> None:
        parsed = parse_decimal(self.rate)
        object.__setattr__(self, "rate", parsed)
        if parsed <= 0:
            raise InvalidInputError("Exchange rate must be positive.")


@dataclass(frozen=True, slots=True)
class RawEvent:
    event_id: str
    user_id: str
    event_type: EventType
    description: str
    category: str
    direction: Direction
    amount: Money | None
    event_date: date
    settlement_date: date | None
    status: EventStatus
    linked_event_id: str | None
    flexibility: Flexibility
    minimum_allowed_amount: Money | None = None
    currency: Currency | None = None

    def __post_init__(self) -> None:
        _required_identifier("event_id", self.event_id)
        _required_identifier("user_id", self.user_id)
        if self.currency is None:
            if self.amount is None:
                raise InvalidInputError("A blank event amount still requires its currency.")
            object.__setattr__(self, "currency", self.amount.currency)
        if not isinstance(self.currency, Currency):
            raise InvalidInputError("Event currency must be a Currency value.")
        if self.amount is not None:
            _nonnegative("event amount", self.amount)
            if self.amount.currency != self.currency:
                raise InvalidInputError("Event currency differs from amount currency.")
        if self.minimum_allowed_amount is not None:
            _nonnegative("minimum_allowed_amount", self.minimum_allowed_amount)
            if self.minimum_allowed_amount.currency != self.currency:
                raise InvalidInputError("Event amount and minimum amount currencies differ.")


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    source_kind: EvidenceSourceKind
    source_id: str
    observed_at: datetime | None
    content_hash: str | None = None
    provider: str | None = None
    schema_version: str = "buywait.evidence.v1"
    source_identity: str | None = None

    def __post_init__(self) -> None:
        _required_identifier("source_id", self.source_id)


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    fact_id: str
    fact_type: EvidenceFactType
    value: EvidenceValue
    provenance: EvidenceProvenance
    related_event_id: str | None = None
    effective_date: date | None = None
    currency: Currency | None = None
    confidence: Decimal = Decimal("1")
    notes: tuple[str, ...] = ()
    subject: str = "event"
    scope: str = "one_time"
    explicit: bool = False
    support_ref: str | None = None

    def __post_init__(self) -> None:
        _required_identifier("fact_id", self.fact_id)
        confidence = parse_decimal(self.confidence)
        object.__setattr__(self, "confidence", confidence)
        if not Decimal(0) <= confidence <= Decimal(1):
            raise InvalidInputError("Evidence confidence must be between zero and one.")


@dataclass(frozen=True, slots=True)
class EventLifecycle:
    lifecycle_id: str
    user_id: str
    event_ids: tuple[str, ...]
    disposition: LifecycleDisposition
    canonical_event_id: str | None
    evidence_fact_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier("lifecycle_id", self.lifecycle_id)
        _required_identifier("user_id", self.user_id)
        if not self.event_ids:
            raise InvalidInputError("An event lifecycle must contain at least one event.")
        if self.canonical_event_id is not None and self.canonical_event_id not in self.event_ids:
            raise InvalidInputError("Canonical event must belong to the lifecycle.")


@dataclass(frozen=True, slots=True)
class CanonicalCashFlow:
    flow_id: str
    user_id: str
    effective_date: date
    amount: Money
    direction: Direction
    category: str
    description: str
    cash_state: CashState
    source_event_ids: tuple[str, ...]
    evidence_fact_ids: tuple[str, ...] = ()
    protected: bool = False
    flexibility: Flexibility = Flexibility.FIXED
    minimum_allowed_amount: Money | None = None
    original_amount: Money | None = None
    fx_rate: Decimal | None = None
    fx_rate_date: date | None = None

    def __post_init__(self) -> None:
        _required_identifier("flow_id", self.flow_id)
        _required_identifier("user_id", self.user_id)
        _nonnegative("cash-flow amount", self.amount)
        if self.direction is Direction.NON_CASH:
            raise InvalidInputError("Canonical cash flow cannot have non-cash direction.")
        if not self.source_event_ids:
            raise InvalidInputError("Canonical cash flow needs source event provenance.")
        if self.minimum_allowed_amount is not None:
            _nonnegative("minimum_allowed_amount", self.minimum_allowed_amount)
            if self.minimum_allowed_amount.currency != self.amount.currency:
                raise InvalidInputError("Cash-flow amount and minimum amount currencies differ.")
        if self.original_amount is not None:
            _nonnegative("original cash-flow amount", self.original_amount)
            if self.original_amount.currency != self.amount.currency:
                rate = parse_decimal(self.fx_rate) if self.fx_rate is not None else None
                if rate is None or rate <= 0 or self.fx_rate_date is None:
                    raise InvalidInputError("Converted cash flow needs a positive FX rate and rate date.")
                object.__setattr__(self, "fx_rate", rate)
            elif self.fx_rate is not None or self.fx_rate_date is not None:
                raise InvalidInputError("Same-currency cash flow cannot carry FX conversion metadata.")
        elif self.fx_rate is not None or self.fx_rate_date is not None:
            raise InvalidInputError("FX metadata requires the original cash-flow amount.")


@dataclass(frozen=True, slots=True)
class EventDisposition:
    """Exactly one auditable ledger outcome for one supplied financial-event row."""

    event_id: str
    lifecycle_id: str
    disposition: LifecycleDisposition
    rule_code: str
    explanation: str
    flow_id: str | None = None
    evidence_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier("event_id", self.event_id)
        _required_identifier("lifecycle_id", self.lifecycle_id)
        _required_identifier("rule_code", self.rule_code)
        _required_identifier("explanation", self.explanation)


@dataclass(frozen=True, slots=True)
class LedgerEvidenceDisposition:
    """How one selected evidence fact affected the canonical ledger."""

    fact_id: str
    action: str
    rule_code: str
    explanation: str
    related_event_id: str | None = None
    flow_id: str | None = None

    def __post_init__(self) -> None:
        _required_identifier("fact_id", self.fact_id)
        if self.action not in {"applied", "deferred_to_recurrence", "excluded", "unresolved"}:
            raise InvalidInputError("Unsupported ledger evidence disposition.")
        _required_identifier("rule_code", self.rule_code)
        _required_identifier("explanation", self.explanation)


@dataclass(frozen=True, slots=True)
class HistoricalSeriesCandidate:
    """A provenance-only history group for Phase 06 recurrence inference."""

    series_id: str
    user_id: str
    category: str
    direction: Direction
    source_flow_ids: tuple[str, ...]
    evidence_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier("series_id", self.series_id)
        _required_identifier("user_id", self.user_id)
        _required_identifier("category", self.category)
        if not self.source_flow_ids:
            raise InvalidInputError("Historical series candidate needs at least one flow.")
        if self.direction is Direction.NON_CASH:
            raise InvalidInputError("Historical cash series cannot be non-cash.")


@dataclass(frozen=True, slots=True)
class LedgerWarning:
    code: str
    message: str
    related_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier("ledger warning code", self.code)
        _required_identifier("ledger warning message", self.message)


@dataclass(frozen=True, slots=True)
class CanonicalLedger:
    """Immutable, request-as-of financial state shared by forecasting and audit surfaces."""

    request_id: str
    user_id: str
    as_of_date: date
    home_currency: Currency
    current_available_balance: Money
    minimum_balance_to_keep: Money
    flows: tuple[CanonicalCashFlow, ...]
    event_dispositions: tuple[EventDisposition, ...]
    evidence_dispositions: tuple[LedgerEvidenceDisposition, ...]
    lifecycles: tuple[EventLifecycle, ...]
    historical_series_candidates: tuple[HistoricalSeriesCandidate, ...]
    warnings: tuple[LedgerWarning, ...]
    fingerprint: str
    schema_version: str = "buywait.canonical-ledger.v1"

    def __post_init__(self) -> None:
        _required_identifier("request_id", self.request_id)
        _required_identifier("user_id", self.user_id)
        if self.schema_version != "buywait.canonical-ledger.v1":
            raise InvalidInputError("Unsupported canonical-ledger schema version.")
        if self.current_available_balance.currency != self.home_currency:
            raise InvalidInputError("Ledger balance must use the home currency.")
        if self.minimum_balance_to_keep.currency != self.home_currency:
            raise InvalidInputError("Ledger minimum balance must use the home currency.")
        if any(flow.amount.currency != self.home_currency for flow in self.flows):
            raise InvalidInputError("Every canonical flow must use the home currency.")
        event_ids = [item.event_id for item in self.event_dispositions]
        if len(event_ids) != len(set(event_ids)):
            raise InvariantViolationError("A financial event received multiple ledger dispositions.")
        flow_ids = {flow.flow_id for flow in self.flows}
        if len(flow_ids) != len(self.flows):
            raise InvariantViolationError("Canonical flow IDs must be unique.")
        if any(item.flow_id is not None and item.flow_id not in flow_ids for item in self.event_dispositions):
            raise InvariantViolationError("An event disposition references an unknown flow.")
        if len(self.fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.fingerprint):
            raise InvalidInputError("Canonical-ledger fingerprint must be lowercase SHA-256.")

    @property
    def historical_flows(self) -> tuple[CanonicalCashFlow, ...]:
        return tuple(flow for flow in self.flows if flow.cash_state is CashState.HISTORICAL)

    @property
    def forecast_flows(self) -> tuple[CanonicalCashFlow, ...]:
        return tuple(flow for flow in self.flows if flow.cash_state is not CashState.HISTORICAL)


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    rule_id: str
    user_id: str
    cadence: RecurrenceCadence
    interval: int
    amount_strategy: AmountStrategy
    amount: Money
    category: str
    direction: Direction
    source_event_ids: tuple[str, ...]
    active_range: DateRange
    anchor_date: date
    confidence: Decimal
    day_of_month: int | None = None
    weekday: int | None = None
    semantic_key: str = ""
    description: str = ""
    protected: bool = False
    flexibility: Flexibility = Flexibility.FIXED
    minimum_allowed_amount: Money | None = None
    evidence_fact_ids: tuple[str, ...] = ()
    end_of_month: bool = False
    observed_count: int = 0

    def __post_init__(self) -> None:
        _required_identifier("rule_id", self.rule_id)
        if self.interval <= 0:
            raise InvalidInputError("Recurrence interval must be positive.")
        _nonnegative("recurring amount", self.amount)
        confidence = parse_decimal(self.confidence)
        object.__setattr__(self, "confidence", confidence)
        if not Decimal(0) <= confidence <= Decimal(1):
            raise InvalidInputError("Recurrence confidence must be between zero and one.")
        if self.day_of_month is not None and not 1 <= self.day_of_month <= 31:
            raise InvalidInputError("day_of_month must be between 1 and 31.")
        if self.weekday is not None and not 0 <= self.weekday <= 6:
            raise InvalidInputError("weekday must be between 0 and 6.")
        if not self.source_event_ids:
            raise InvalidInputError("Recurrence rule needs source event provenance.")
        if not self.semantic_key:
            object.__setattr__(self, "semantic_key", self.category)
        if not self.description:
            object.__setattr__(self, "description", self.category)
        if self.minimum_allowed_amount is not None:
            _nonnegative("recurrence minimum amount", self.minimum_allowed_amount)
            if self.minimum_allowed_amount.currency != self.amount.currency:
                raise InvalidInputError("Recurrence amount and minimum amount currencies differ.")
        if self.end_of_month and self.cadence is not RecurrenceCadence.MONTHLY:
            raise InvalidInputError("End-of-month behavior requires monthly cadence.")
        if self.observed_count < 0:
            raise InvalidInputError("Observed recurrence count cannot be negative.")


@dataclass(frozen=True, slots=True)
class ForecastOccurrence:
    flow: CanonicalCashFlow
    origin: ForecastOrigin
    recurrence_rule_id: str | None = None

    def __post_init__(self) -> None:
        if self.origin is ForecastOrigin.RECURRENCE and self.recurrence_rule_id is None:
            raise InvalidInputError("A recurring occurrence must reference its rule.")
        if self.origin is ForecastOrigin.CANONICAL_FUTURE and self.recurrence_rule_id is not None:
            raise InvalidInputError("A canonical future occurrence cannot reference a recurrence rule.")


@dataclass(frozen=True, slots=True)
class ForecastTimeline:
    request_id: str
    user_id: str
    home_currency: Currency
    horizon: DateRange
    recurrence_rules: tuple[RecurrenceRule, ...]
    occurrences: tuple[ForecastOccurrence, ...]
    unresolved_event_ids: tuple[str, ...]
    fingerprint: str
    schema_version: str = "buywait.forecast-timeline.v1"

    def __post_init__(self) -> None:
        _required_identifier("request_id", self.request_id)
        _required_identifier("user_id", self.user_id)
        if self.schema_version != "buywait.forecast-timeline.v1":
            raise InvalidInputError("Unsupported forecast-timeline schema version.")
        if tuple(sorted(self.occurrences, key=lambda item: (
            item.flow.effective_date, item.flow.flow_id
        ))) != self.occurrences:
            raise InvalidInputError("Forecast occurrences must be chronological.")
        flow_ids = [item.flow.flow_id for item in self.occurrences]
        if len(flow_ids) != len(set(flow_ids)):
            raise InvariantViolationError("Forecast occurrence IDs must be unique.")
        rule_ids = {rule.rule_id for rule in self.recurrence_rules}
        if len(rule_ids) != len(self.recurrence_rules):
            raise InvariantViolationError("Forecast recurrence-rule IDs must be unique.")
        for occurrence in self.occurrences:
            flow = occurrence.flow
            if flow.user_id != self.user_id or flow.amount.currency != self.home_currency:
                raise InvalidInputError("Forecast occurrence differs from timeline user or currency.")
            if not self.horizon.contains(flow.effective_date):
                raise InvalidInputError("Forecast occurrence lies outside the horizon.")
            if occurrence.recurrence_rule_id is not None and occurrence.recurrence_rule_id not in rule_ids:
                raise InvariantViolationError("Forecast occurrence references an unknown rule.")
        if len(self.fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.fingerprint):
            raise InvalidInputError("Forecast fingerprint must be lowercase SHA-256.")


@dataclass(frozen=True, slots=True)
class Payment:
    payment_date: date
    amount: Money
    sequence: int

    def __post_init__(self) -> None:
        if self.amount.amount <= 0:
            raise InvalidInputError("Payment amount must be positive.")
        if self.sequence <= 0:
            raise InvalidInputError("Payment sequence must be positive.")


@dataclass(frozen=True, slots=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: PaymentMethod
    payment_amount: Money
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int | None
    financing_fee: Money
    total_payable_amount: Money

    def __post_init__(self) -> None:
        _required_identifier("payment_option_id", self.payment_option_id)
        _required_identifier("request_id", self.request_id)
        _same_currency(
            "Payment option values",
            (self.payment_amount, self.financing_fee, self.total_payable_amount),
        )
        for name, value in (
            ("payment_amount", self.payment_amount),
            ("financing_fee", self.financing_fee),
            ("total_payable_amount", self.total_payable_amount),
        ):
            _nonnegative(name, value)
        if self.number_of_payments <= 0:
            raise InvalidInputError("number_of_payments must be positive.")
        if self.payment_method not in {PaymentMethod.FULL_PAYMENT, PaymentMethod.INSTALLMENTS}:
            raise InvalidInputError("Supplied options must be full payment or installments.")
        if self.payment_method is PaymentMethod.FULL_PAYMENT:
            if self.number_of_payments != 1 or self.payment_frequency_days is not None:
                raise InvalidInputError("Full-payment option must have one payment and no frequency.")
        elif self.payment_frequency_days is None or self.payment_frequency_days <= 0:
            raise InvalidInputError("Installment option needs a positive payment frequency.")


@dataclass(frozen=True, slots=True)
class EvaluationBundle:
    """Complete normalized input boundary for evaluating one request."""

    request: FinancialRequest
    profile: Profile
    events: tuple[RawEvent, ...]
    payment_options: tuple[PaymentOption, ...]
    messages: tuple[MessageRecord, ...]
    images: tuple[ImageRecord, ...]
    exchange_rates: tuple[ExchangeRate, ...]

    def __post_init__(self) -> None:
        if self.request.user_id != self.profile.user_id:
            raise InvalidInputError("Request and profile users differ.")
        if self.request.requested_amount.currency != self.profile.home_currency:
            raise InvalidInputError("Request amount must use the user's home currency.")
        for event in self.events:
            if event.user_id != self.profile.user_id:
                raise InvalidInputError("Evaluation bundle contains another user's event.")
        for option in self.payment_options:
            if option.request_id != self.request.request_id:
                raise InvalidInputError("Evaluation bundle contains another request's option.")
            if option.total_payable_amount.currency != self.profile.home_currency:
                raise InvalidInputError("Payment option must use the user's home currency.")
        for message in self.messages:
            if message.user_id != self.profile.user_id:
                raise InvalidInputError("Evaluation bundle contains another user's message.")
        for image in self.images:
            if image.user_id != self.profile.user_id:
                raise InvalidInputError("Evaluation bundle contains another user's image.")


@dataclass(frozen=True, slots=True)
class SpendingChange:
    action: SpendingChangeAction
    event_id: str
    new_amount: Money | None = None

    def __post_init__(self) -> None:
        _required_identifier("event_id", self.event_id)
        if self.action is SpendingChangeAction.STOP and self.new_amount is not None:
            raise InvalidInputError("Stop action cannot include a new amount.")
        if self.action is SpendingChangeAction.REDUCE_TO and self.new_amount is None:
            raise InvalidInputError("Reduce action requires a new amount.")
        if self.new_amount is not None:
            _nonnegative("spending change amount", self.new_amount)


@dataclass(frozen=True, slots=True)
class RuleViolation:
    code: ViolationCode
    message: str
    violation_date: date | None = None
    related_id: str | None = None


@dataclass(frozen=True, slots=True)
class ForecastDay:
    forecast_date: date
    opening_balance: Money
    inflows: tuple[CanonicalCashFlow, ...]
    outflows: tuple[CanonicalCashFlow, ...]
    payments: tuple[Payment, ...]
    closing_balance: Money
    minimum_balance_to_keep: Money
    lowest_intraday_balance: Money | None = None

    def __post_init__(self) -> None:
        monetary_values = (
            self.opening_balance,
            self.closing_balance,
            self.minimum_balance_to_keep,
            *(value for value in (self.lowest_intraday_balance,) if value is not None),
            *(flow.amount for flow in self.inflows),
            *(flow.amount for flow in self.outflows),
            *(payment.amount for payment in self.payments),
        )
        _same_currency("Forecast day values", monetary_values)


@dataclass(frozen=True, slots=True)
class ForecastProofPoint:
    forecast_date: date
    checkpoint: ForecastCheckpointKind
    balance: Money
    related_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SafetyResult:
    safe: bool
    minimum_projected_balance: Money
    minimum_balance_to_keep: Money
    tightest_date: date | None
    violations: tuple[RuleViolation, ...] = ()
    forecast_fingerprint: str = ""

    def __post_init__(self) -> None:
        _same_currency(
            "Safety result values",
            (self.minimum_projected_balance, self.minimum_balance_to_keep),
        )
        if self.safe and self.violations:
            raise InvariantViolationError("A safe result cannot contain violations.")
        if self.safe and self.minimum_projected_balance.amount < self.minimum_balance_to_keep.amount:
            raise InvariantViolationError("Safe result falls below the minimum balance.")


@dataclass(frozen=True, slots=True)
class ForecastSimulation:
    request_id: str
    days: tuple[ForecastDay, ...]
    safety_result: SafetyResult
    proof: tuple[ForecastProofPoint, ...]
    fingerprint: str
    schema_version: str = "buywait.forecast-simulation.v1"

    def __post_init__(self) -> None:
        _required_identifier("request_id", self.request_id)
        if self.schema_version != "buywait.forecast-simulation.v1":
            raise InvalidInputError("Unsupported forecast-simulation schema version.")
        if not self.days:
            raise InvalidInputError("Forecast simulation needs at least one day.")
        if tuple(sorted(self.days, key=lambda item: item.forecast_date)) != self.days:
            raise InvalidInputError("Forecast days must be chronological.")
        if len({item.forecast_date for item in self.days}) != len(self.days):
            raise InvariantViolationError("Forecast simulation has duplicate dates.")
        if len(self.fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.fingerprint):
            raise InvalidInputError("Simulation fingerprint must be lowercase SHA-256.")


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    request_id: str
    requested_amount: Money
    amount_safe_to_pay: Money
    earliest_date_for_full_payment: date | None
    timeline: ForecastTimeline
    baseline: ForecastSimulation
    schema_version: str = "buywait.capacity-assessment.v1"

    def __post_init__(self) -> None:
        _required_identifier("request_id", self.request_id)
        if self.schema_version != "buywait.capacity-assessment.v1":
            raise InvalidInputError("Unsupported capacity-assessment schema version.")
        _same_currency("Capacity amounts", (self.requested_amount, self.amount_safe_to_pay))
        _nonnegative("requested_amount", self.requested_amount)
        _nonnegative("amount_safe_to_pay", self.amount_safe_to_pay)
        if self.amount_safe_to_pay.amount > self.requested_amount.amount:
            raise InvalidInputError("Safe capacity cannot exceed the requested amount.")


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    candidate_id: str
    request_id: str
    payment_method: PaymentMethod
    payments: tuple[Payment, ...]
    spending_changes: tuple[SpendingChange, ...]
    total_payable_amount: Money
    payment_option_id: str | None = None
    eligible: bool = True
    completion_date: date | None = None
    safety_result: SafetyResult | None = None
    rejection_codes: tuple[CandidateRejectionCode, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier("candidate_id", self.candidate_id)
        _required_identifier("request_id", self.request_id)
        _nonnegative("total_payable_amount", self.total_payable_amount)
        if tuple(sorted(self.payments, key=lambda item: (item.payment_date, item.sequence))) != self.payments:
            raise InvalidInputError("Candidate payments must be chronological.")
        if self.payments:
            currency = _same_currency("Candidate payments", tuple(item.amount for item in self.payments))
            if currency != self.total_payable_amount.currency:
                raise InvalidInputError("Candidate total currency differs from payment currency.")
            payment_sum = sum((item.amount.amount for item in self.payments), Decimal(0))
            if payment_sum != self.total_payable_amount.amount:
                raise InvalidInputError("Candidate payment amounts do not equal total payable amount.")
            actual_completion = self.payments[-1].payment_date
            if self.completion_date is not None and self.completion_date != actual_completion:
                raise InvalidInputError("Candidate completion date differs from final payment date.")
        if len(self.spending_changes) > 3:
            raise InvalidInputError("A candidate can contain at most three spending changes.")
        changed_events = [item.event_id for item in self.spending_changes]
        if len(changed_events) != len(set(changed_events)):
            raise InvalidInputError("A candidate cannot change the same event twice.")


@dataclass(frozen=True, slots=True)
class Decision:
    request_id: str
    request_date: date
    requested_amount: Money
    amount_safe_to_pay: Money
    affordability_status: AffordabilityStatus
    recommended_payment_method: PaymentMethod
    payment_plan: tuple[Payment, ...]
    earliest_date_for_full_payment: date | None
    spending_changes_needed: tuple[SpendingChange, ...]
    decision_explanation: str
    selected_candidate_id: str | None = None
    audit_trace_id: str | None = None
    schema_version: str = "buywait.decision.v1"

    def __post_init__(self) -> None:
        _required_identifier("request_id", self.request_id)
        _required_identifier("decision_explanation", self.decision_explanation)
        if self.schema_version != "buywait.decision.v1":
            raise InvalidInputError("Unsupported decision schema version.")
        _same_currency("Decision amounts", (self.requested_amount, self.amount_safe_to_pay))
        _nonnegative("requested_amount", self.requested_amount)
        _nonnegative("amount_safe_to_pay", self.amount_safe_to_pay)
        if self.amount_safe_to_pay.amount > self.requested_amount.amount:
            raise InvalidInputError("Safe amount cannot exceed requested amount.")
        if tuple(sorted(self.payment_plan, key=lambda item: (item.payment_date, item.sequence))) != self.payment_plan:
            raise InvalidInputError("Decision payment plan must be chronological.")
        for payment in self.payment_plan:
            if payment.amount.currency != self.requested_amount.currency:
                raise InvalidInputError("Decision payment currency differs from request currency.")
        if len(self.spending_changes_needed) > 3:
            raise InvalidInputError("Decision can contain at most three spending changes.")
        changed_events = [item.event_id for item in self.spending_changes_needed]
        if len(changed_events) != len(set(changed_events)):
            raise InvalidInputError("A decision cannot change the same event twice.")
        if self.affordability_status is AffordabilityStatus.AFFORDABLE_NOW:
            if self.earliest_date_for_full_payment != self.request_date:
                raise InvalidInputError("Affordable-now decision must have request date as earliest full-payment date.")
            if self.recommended_payment_method is not PaymentMethod.FULL_PAYMENT:
                raise InvalidInputError("Affordable-now decision must recommend full payment.")
        allowed_statuses = {
            PaymentMethod.FULL_PAYMENT: {
                AffordabilityStatus.AFFORDABLE_NOW,
                AffordabilityStatus.AFFORDABLE_WITH_PLAN,
            },
            PaymentMethod.PARTIAL_PAYMENT: {AffordabilityStatus.AFFORDABLE_WITH_PLAN},
            PaymentMethod.INSTALLMENTS: {AffordabilityStatus.AFFORDABLE_WITH_PLAN},
            PaymentMethod.WAIT: {AffordabilityStatus.AFFORDABLE_LATER},
            PaymentMethod.NOT_RECOMMENDED: {AffordabilityStatus.NOT_AFFORDABLE},
        }
        if self.affordability_status not in allowed_statuses[self.recommended_payment_method]:
            raise InvalidInputError("Affordability status and payment method are inconsistent.")
        if self.recommended_payment_method is PaymentMethod.NOT_RECOMMENDED:
            if self.payment_plan:
                raise InvalidInputError("A not-recommended decision cannot contain payments.")
        elif not self.payment_plan:
            raise InvalidInputError("A recommended method must contain its payment plan.")
        if self.recommended_payment_method is PaymentMethod.FULL_PAYMENT:
            if len(self.payment_plan) != 1 or self.payment_plan[0].payment_date != self.request_date:
                raise InvalidInputError("Full payment must be one payment on request date.")
        if self.recommended_payment_method is PaymentMethod.WAIT:
            if (
                len(self.payment_plan) != 1
                or self.earliest_date_for_full_payment is None
                or self.payment_plan[0].payment_date != self.earliest_date_for_full_payment
            ):
                raise InvalidInputError("Wait must contain one payment on the earliest safe date.")
        if self.recommended_payment_method is PaymentMethod.PARTIAL_PAYMENT:
            if not Decimal(0) < self.amount_safe_to_pay.amount < self.requested_amount.amount:
                raise InvalidInputError("Partial payment needs a positive safe amount below the request.")
            if len(self.payment_plan) != 2 or self.earliest_date_for_full_payment is None:
                raise InvalidInputError("Partial payment needs exactly two dated payments.")
            first, second = self.payment_plan
            if first.payment_date != self.request_date or first.amount != self.amount_safe_to_pay:
                raise InvalidInputError("Partial payment must pay the safe amount on request date.")
            if second.payment_date != self.earliest_date_for_full_payment:
                raise InvalidInputError("Partial remainder date must equal earliest full-payment date.")
        if self.recommended_payment_method is PaymentMethod.INSTALLMENTS and len(self.payment_plan) < 2:
            raise InvalidInputError("Installments require at least two payments.")
        for change in self.spending_changes_needed:
            if change.new_amount is not None and change.new_amount.currency != self.requested_amount.currency:
                raise InvalidInputError("Spending change currency differs from request currency.")
        if self.recommended_payment_method in {
            PaymentMethod.FULL_PAYMENT,
            PaymentMethod.PARTIAL_PAYMENT,
            PaymentMethod.WAIT,
        }:
            payment_total = sum((item.amount.amount for item in self.payment_plan), Decimal(0))
            if payment_total != self.requested_amount.amount:
                raise InvalidInputError("Recommended payments do not complete the requested amount.")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    operation: str
    provider: str
    model: str
    request_id: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    latency_ms: int
    cache_hit: bool
    recorded_at: datetime

    def __post_init__(self) -> None:
        _required_identifier("operation", self.operation)
        _required_identifier("provider", self.provider)
        _required_identifier("model", self.model)
        if min(self.input_tokens, self.output_tokens, self.latency_ms) < 0:
            raise InvalidInputError("Usage counters cannot be negative.")
        cost = parse_decimal(self.estimated_cost_usd)
        object.__setattr__(self, "estimated_cost_usd", cost)
        if cost < 0:
            raise InvalidInputError("Estimated cost cannot be negative.")

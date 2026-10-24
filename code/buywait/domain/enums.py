"""Closed vocabularies from the challenge and internal contracts."""

from enum import StrEnum


class Currency(StrEnum):
    EUR = "EUR"
    IDR = "IDR"
    INR = "INR"
    USD = "USD"
    ZAR = "ZAR"


class RequestType(StrEnum):
    PURCHASE = "purchase"
    TRAVEL = "travel"
    EDUCATION = "education"
    FAMILY_TRANSFER = "family_transfer"
    DEBT_REPAYMENT = "debt_repayment"
    INVESTMENT = "investment"
    HOUSING = "housing"
    EMERGENCY_EXPENSE = "emergency_expense"
    OTHER = "other"


class AffordabilityStatus(StrEnum):
    AFFORDABLE_NOW = "affordable_now"
    AFFORDABLE_WITH_PLAN = "affordable_with_plan"
    AFFORDABLE_LATER = "affordable_later"
    NOT_AFFORDABLE = "not_affordable"


class PaymentMethod(StrEnum):
    FULL_PAYMENT = "full_payment"
    PARTIAL_PAYMENT = "partial_payment"
    INSTALLMENTS = "installments"
    WAIT = "wait"
    NOT_RECOMMENDED = "not_recommended"


class EventType(StrEnum):
    EXPENSE = "expense"
    SUBSCRIPTION = "subscription"
    INCOME = "income"
    DEBT_PAYMENT = "debt_payment"
    INVESTMENT_PURCHASE = "investment_purchase"
    INVESTMENT_VALUATION = "investment_valuation"
    INVESTMENT_SALE = "investment_sale"
    REFUND = "refund"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    NON_CASH = "non_cash"


class EventStatus(StrEnum):
    SETTLED = "settled"
    PENDING = "pending"
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNREALIZED = "unrealized"


class Flexibility(StrEnum):
    FIXED = "fixed"
    REDUCIBLE = "reducible"
    STOPPABLE = "stoppable"
    REDUCIBLE_OR_STOPPABLE = "reducible_or_stoppable"


class EvidenceSourceKind(StrEnum):
    CSV_EVENT = "csv_event"
    MESSAGE = "message"
    IMAGE = "image"
    OCR = "ocr"
    MODEL = "model"
    USER_CORRECTION = "user_correction"


class MessageSource(StrEnum):
    EMPLOYER = "employer"
    SERVICE_PROVIDER = "service_provider"
    FINANCIAL_SERVICE = "financial_service"
    BANK = "bank"
    MERCHANT = "merchant"


class EvidenceFactType(StrEnum):
    AMOUNT = "amount"
    CURRENCY = "currency"
    EFFECTIVE_DATE = "effective_date"
    SETTLEMENT_DATE = "settlement_date"
    STATUS = "status"
    RECURRENCE = "recurrence"
    AMENDMENT = "amendment"
    CANCELLATION = "cancellation"
    SETTLEMENT = "settlement"
    INTERNAL_TRANSFER = "internal_transfer"
    CASH_STATE = "cash_state"


class LifecycleDisposition(StrEnum):
    HISTORICAL_BALANCE = "historical_balance"
    FUTURE_DEBIT = "future_debit"
    FUTURE_CONFIRMED_CREDIT = "future_confirmed_credit"
    PENDING_CREDIT_IGNORED = "pending_credit_ignored"
    FAILED_OR_CANCELLED = "failed_or_cancelled"
    DUPLICATE = "duplicate"
    INTERNAL_TRANSFER = "internal_transfer"
    NON_CASH = "non_cash"
    UNRESOLVED = "unresolved"


class CashState(StrEnum):
    HISTORICAL = "historical"
    FUTURE_CONFIRMED = "future_confirmed"
    FUTURE_RESERVED = "future_reserved"


class ForecastOrigin(StrEnum):
    CANONICAL_FUTURE = "canonical_future"
    RECURRENCE = "recurrence"


class ForecastCheckpointKind(StrEnum):
    OPENING = "opening"
    PRE_CREDIT = "pre_credit"
    CLOSING = "closing"


class RecurrenceCadence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    CUSTOM_DAYS = "custom_days"


class AmountStrategy(StrEnum):
    LATEST = "latest"
    FIXED = "fixed"
    CONSERVATIVE_ROBUST = "conservative_robust"


class SpendingChangeAction(StrEnum):
    STOP = "stop"
    REDUCE_TO = "reduce_to"


class ViolationCode(StrEnum):
    BELOW_MINIMUM_BALANCE = "below_minimum_balance"
    AFTER_COMPLETION_DEADLINE = "after_completion_deadline"
    INELIGIBLE_METHOD = "ineligible_method"
    INVALID_PAYMENT_TOTAL = "invalid_payment_total"
    INVALID_OPTION_SCHEDULE = "invalid_option_schedule"
    ILLEGAL_SPENDING_CHANGE = "illegal_spending_change"
    UNRESOLVED_EVIDENCE = "unresolved_evidence"


class CandidateRejectionCode(StrEnum):
    METHOD_NOT_ACCEPTED = "method_not_accepted"
    PARTIAL_NOT_ALLOWED = "partial_not_allowed"
    OPTION_NOT_SUPPLIED = "option_not_supplied"
    INSTALLMENT_TERM_EXCEEDED = "installment_term_exceeded"
    AFTER_DEADLINE = "after_deadline"
    NO_SAFE_DATE = "no_safe_date"
    PAYMENT_SUM_MISMATCH = "payment_sum_mismatch"
    ILLEGAL_CHANGE = "illegal_change"
    BELOW_FLOOR = "below_floor"
    UNRESOLVED_EVIDENCE = "unresolved_evidence"
    INVARIANT_FAILURE = "invariant_failure"


class ProviderKind(StrEnum):
    EVIDENCE = "evidence"
    OCR = "ocr"
    MODEL = "model"
    IMPORT = "import"
    EXPORT = "export"
    STORAGE = "storage"
    TELEMETRY = "telemetry"
    POLICY = "policy"

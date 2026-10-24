"""Explicit participant CSV headers. No schema inferred from data values."""

from buywait.domain.serialization import OUTPUT_COLUMNS

REQUEST_COLUMNS = (
    "request_id", "user_id", "request_date", "request_type", "requested_amount",
    "desired_completion_date", "allows_partial_payment", "request_text",
)
HEADERS = {
    "financial_profiles.csv": (
        "user_id", "home_currency", "current_available_balance", "minimum_balance_to_keep",
        "financial_priorities", "expense_categories_to_protect",
        "expense_categories_user_is_willing_to_reduce", "expense_categories_user_is_willing_to_stop",
        "payment_methods_user_will_consider", "max_installment_months",
    ),
    "requests.csv": REQUEST_COLUMNS,
    "sample_requests.csv": REQUEST_COLUMNS + OUTPUT_COLUMNS[1:],
    "financial_events.csv": (
        "event_id", "user_id", "event_type", "description", "category", "direction", "amount",
        "currency", "event_date", "settlement_date", "status", "linked_event_id", "flexibility",
        "minimum_allowed_amount",
    ),
    "exchange_rates.csv": ("rate_date", "from_currency", "to_currency", "rate"),
    "request_payment_options.csv": (
        "payment_option_id", "request_id", "payment_method", "payment_amount", "number_of_payments",
        "first_payment_date", "payment_frequency_days", "financing_fee", "total_payable_amount",
    ),
    "messages.csv": (
        "message_id", "user_id", "request_id", "related_event_id", "sent_at", "source_type", "message_text",
    ),
    "images.csv": ("image_id", "user_id", "request_id", "related_event_id"),
    "output.csv": OUTPUT_COLUMNS,
}

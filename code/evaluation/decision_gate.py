"""Phase 07 decision, ranking, explanation, and output-contract gate."""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from buywait.adapters.dataset import load_dataset
from buywait.application import BuyWaitDecisionService
from buywait.domain.serialization import decision_to_csv_row, format_payment_plan, format_spending_changes
from buywait.evidence.service import EvidenceService
from buywait.planning import write_output_atomic
from buywait.providers.local_ocr import local_ocr


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate final financial decisions")
    parser.add_argument("--dataset-dir")
    parser.add_argument("--ocr", choices=("auto", "rapidocr", "windows", "tesseract", "none"), default="auto")
    parser.add_argument("--samples-only", action="store_true")
    parser.add_argument("--debug-request")
    parser.add_argument("--report")
    parser.add_argument("--write-output")
    args = parser.parse_args()

    context = load_dataset(args.dataset_dir)
    service = BuyWaitDecisionService(
        context,
        evidence_service=EvidenceService(context, ocr=local_ocr(args.ocr), model=None, cache=None),
    )
    requests = context.sample_requests if args.samples_only else context.evaluation_requests + context.sample_requests
    if args.debug_request:
        requests = tuple(item for item in requests if item.request_id == args.debug_request)
        if not requests:
            parser.error("--debug-request is outside the selected scope")

    failures: list[dict] = []
    public: list[dict] = []
    results = []
    methods: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    candidate_count = 0
    unresolved = []
    fingerprints: set[str] = set()

    for request in requests:
        try:
            result = service.decide(request.request_id)
            results.append(result)
            decision, search = result.decision, result.search
            row = decision_to_csv_row(decision)
            checks = {
                "safe_amount_bounds": Decimal(0) <= decision.amount_safe_to_pay.amount <= request.requested_amount.amount,
                "explanation_present": bool(decision.decision_explanation.strip()),
                "exact_columns": len(row) == 8,
                "winner_consistent": (search.winner is None) == (decision.selected_candidate_id is None),
                "winner_safe": search.winner is None or bool(search.winner.safety_result and search.winner.safety_result.safe),
                "deadline": all(item.payment_date <= request.desired_completion_date for item in decision.payment_plan),
            }
            if not all(checks.values()):
                failures.append({"request_id": request.request_id,
                                 "failed_checks": sorted(key for key, passed in checks.items() if not passed)})
            methods[decision.recommended_payment_method.value] += 1
            statuses[decision.affordability_status.value] += 1
            rejection_counts.update(search.rejection_counts)
            candidate_count += len(search.candidates)
            fingerprints.add(search.fingerprint)
            if result.assessment.timeline.unresolved_event_ids:
                unresolved.append({"request_id": request.request_id,
                                   "event_ids": list(result.assessment.timeline.unresolved_event_ids)})

            expected = context.sample_expectations.get(request.request_id)
            if expected:
                public.append({
                    "request_id": request.request_id,
                    "actual_safe_amount": str(decision.amount_safe_to_pay.amount),
                    "expected_safe_amount": str(expected.amount_safe_to_pay.amount),
                    "actual_status": decision.affordability_status.value,
                    "expected_status": expected.affordability_status.value,
                    "actual_method": decision.recommended_payment_method.value,
                    "expected_method": expected.recommended_payment_method.value,
                    "actual_payment_plan": format_payment_plan(decision.payment_plan),
                    "expected_payment_plan": format_payment_plan(expected.payment_plan),
                    "actual_earliest_date": (decision.earliest_date_for_full_payment.isoformat()
                                             if decision.earliest_date_for_full_payment else None),
                    "expected_earliest_date": (expected.earliest_date_for_full_payment.isoformat()
                                               if expected.earliest_date_for_full_payment else None),
                    "actual_spending_changes": format_spending_changes(decision.spending_changes_needed),
                    "expected_spending_changes": format_spending_changes(expected.spending_changes_needed),
                    "safe_amount_exact": decision.amount_safe_to_pay == expected.amount_safe_to_pay,
                    "status_exact": decision.affordability_status is expected.affordability_status,
                    "method_exact": decision.recommended_payment_method is expected.recommended_payment_method,
                    "payment_plan_exact": decision.payment_plan == expected.payment_plan,
                    "earliest_date_exact": decision.earliest_date_for_full_payment == expected.earliest_date_for_full_payment,
                    "spending_changes_exact": decision.spending_changes_needed == expected.spending_changes_needed,
                })
        except Exception as error:  # Gate records one bounded error per request and continues.
            failures.append({"request_id": request.request_id, "failed_checks": [
                "exception:" + type(error).__name__ + ":" + str(error)[:200]
            ]})

    metric_names = (
        "safe_amount_exact", "status_exact", "method_exact", "payment_plan_exact",
        "earliest_date_exact", "spending_changes_exact",
    )
    report = {
        "schema_version": "buywait.decision-gate.v1",
        "passed": not failures,
        "scope": "samples" if args.samples_only else "all_requests_and_samples",
        "requests": len(requests),
        "decisions": len(results),
        "candidate_count": candidate_count,
        "search_fingerprints": len(fingerprints),
        "methods": dict(sorted(methods.items())),
        "statuses": dict(sorted(statuses.items())),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "unresolved_fail_closed": unresolved,
        "public_sample_metrics": {
            "cases": len(public),
            **{name: sum(item[name] for item in public) for name in metric_names},
        },
        "public_sample_deltas": public,
        "failures": failures,
        "network_model_calls": 0,
    }

    if args.write_output:
        if args.samples_only or args.debug_request:
            parser.error("--write-output requires the complete non-debug scope")
        if failures:
            report["output_error"] = "not_written_because_gate_failed"
        else:
            output = Path(args.write_output)
            if not output.is_absolute():
                output = CODE_DIR.parent / output
            evaluation_ids = {item.request_id for item in context.evaluation_requests}
            evaluation_decisions = [
                item.decision for item in results if item.decision.request_id in evaluation_ids
            ]
            if len(evaluation_decisions) != len(context.evaluation_requests):
                report["output_error"] = "not_written_because_evaluation_rows_are_incomplete"
                report["passed"] = False
            else:
                write_output_atomic(output, evaluation_decisions, dataset_dir=context.dataset_dir)
                report["output"] = str(output)
                report["output_rows"] = len(evaluation_decisions)

    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.report:
        target = Path(args.report)
        if not target.is_absolute():
            target = CODE_DIR.parent / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

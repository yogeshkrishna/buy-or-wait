"""Competition CLI composition root and offline input diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections.abc import Sequence

from buywait import __version__
from buywait.adapters.dataset import DatasetValidationError, load_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="buywait", description="Buy or Wait financial agent")
    parser.add_argument("--version", action="store_true", help="show the contract package version")
    parser.add_argument("--dataset-dir", help="dataset directory; relative paths resolve against the repository")
    parser.add_argument("--validate-inputs", action="store_true", help="validate every participant file and relationship")
    parser.add_argument("--profile-inputs", action="store_true", help="validate and print a privacy-safe JSON input summary")
    parser.add_argument("--evidence-preflight", action="store_true", help="redacted evidence readiness for evaluation and sample requests")
    parser.add_argument("--evidence-trace", metavar="REQUEST_ID", help="redacted one-request evidence trace")
    parser.add_argument("--ledger-trace", metavar="REQUEST_ID", help="redacted canonical-ledger trace for one request")
    parser.add_argument("--ledger-user-trace", metavar="USER_ID", help="redacted canonical-ledger trace for one user's request")
    parser.add_argument("--forecast-trace", metavar="REQUEST_ID", help="redacted 90-day capacity trace for one request")
    parser.add_argument("--forecast-user-trace", metavar="USER_ID", help="redacted 90-day capacity trace for one user's request")
    parser.add_argument("--decision-trace", metavar="REQUEST_ID", help="redacted verified final-decision trace")
    parser.add_argument("--generate-output", action="store_true", help="generate and validate root output.csv")
    parser.add_argument("--output-csv", default="output.csv", help="prediction path relative to repository root")
    parser.add_argument("--validate-output", nargs="?", const="output.csv", metavar="PATH",
                        help="independently replay every decision in an output CSV")
    parser.add_argument("--ocr", choices=("auto", "rapidocr", "windows", "tesseract", "none"), default="auto")
    parser.add_argument("--evidence-model", choices=("none", "openai"), default="none", help="explicitly opt into a remote evidence provider")
    parser.add_argument("--evidence-cache-dir", help="private generated cache directory, relative to repository")
    parser.add_argument("--no-evidence-cache", action="store_true")
    parser.add_argument("--evidence-usage-log", help="append-only model usage log; defaults to ignored audit/evidence-usage.jsonl")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if (args.validate_inputs or args.profile_inputs or args.evidence_preflight or args.evidence_trace
            or args.ledger_trace or args.ledger_user_trace or args.forecast_trace
            or args.forecast_user_trace or args.decision_trace or args.generate_output
            or args.validate_output):
        try:
            context = load_dataset(args.dataset_dir)
        except DatasetValidationError as error:
            print(json.dumps(error.as_dict(), sort_keys=True, ensure_ascii=True), file=sys.stderr)
            return 2
        if args.decision_trace or args.generate_output or args.validate_output:
            from collections import Counter
            from buywait.application import BuyWaitDecisionService
            from buywait.evidence.service import EvidenceService
            from buywait.planning import read_output, redacted_decision_trace, validate_output, write_output_atomic
            from buywait.providers.local_ocr import local_ocr
            entry_dir = Path(__file__).resolve().parent
            root = entry_dir.parent if entry_dir.name == "code" else entry_dir
            service = BuyWaitDecisionService(
                context,
                evidence_service=EvidenceService(context, ocr=local_ocr(args.ocr), model=None, cache=None),
            )
            if args.decision_trace:
                if args.decision_trace not in context.requests_by_id:
                    print(json.dumps({"code": "unknown_request_id"}), file=sys.stderr)
                    return 2
                result = service.decide(args.decision_trace)
                print(json.dumps(redacted_decision_trace(result), sort_keys=True, indent=2))
                return 3 if result.assessment.timeline.unresolved_event_ids else 0
            if args.generate_output:
                output_path = Path(args.output_csv)
                if not output_path.is_absolute():
                    output_path = root / output_path
                results = [service.decide(item.request_id) for item in context.evaluation_requests]
                decisions = [item.decision for item in results]
                write_output_atomic(output_path, decisions, dataset_dir=context.dataset_dir)
                methods = Counter(item.recommended_payment_method.value for item in decisions)
                unresolved = sum(bool(item.assessment.timeline.unresolved_event_ids) for item in results)
                roundtrip = read_output(output_path, context)
                print(json.dumps({
                    "valid": len(roundtrip) == len(decisions),
                    "output": str(output_path),
                    "rows": len(roundtrip),
                    "methods": dict(sorted(methods.items())),
                    "unresolved_fail_closed": unresolved,
                    "network_model_calls": 0,
                    "errors": [],
                }, sort_keys=True, indent=2))
                return 0
            output_path = Path(args.validate_output)
            if not output_path.is_absolute():
                output_path = root / output_path
            report = validate_output(output_path, context, service)
            print(json.dumps({"valid": report.valid, "rows": report.row_count,
                              "errors": list(report.errors[:20])}, sort_keys=True, indent=2))
            return 0 if report.valid else 4
        if args.forecast_trace or args.forecast_user_trace:
            from buywait.evidence.service import EvidenceService
            from buywait.forecasting import ForecastEngine, redacted_forecast_trace
            from buywait.ledger import CanonicalLedgerBuilder
            from buywait.providers.local_ocr import local_ocr
            if args.forecast_trace and args.forecast_user_trace:
                print(json.dumps({"code": "conflicting_trace_targets"}), file=sys.stderr)
                return 2
            request_id = args.forecast_trace
            if args.forecast_user_trace:
                requests = context.requests_by_user_id.get(args.forecast_user_trace, ())
                if len(requests) != 1:
                    print(json.dumps({"code": "user_request_not_unique", "user_id": args.forecast_user_trace,
                                      "request_count": len(requests)}), file=sys.stderr)
                    return 2
                request_id = requests[0].request_id
            if request_id not in context.requests_by_id:
                print(json.dumps({"code": "unknown_request_id"}), file=sys.stderr)
                return 2
            service = EvidenceService(context, ocr=local_ocr(args.ocr), model=None, cache=None)
            evidence = service.evaluate(request_id)
            ledger = CanonicalLedgerBuilder().build(context.bundle(request_id), evidence)
            assessment = ForecastEngine().assess(ledger, context.requests_by_id[request_id].requested_amount,
                                                 evidence.facts)
            print(json.dumps(redacted_forecast_trace(assessment), sort_keys=True, indent=2))
            return 3 if assessment.timeline.unresolved_event_ids else 0
        if args.ledger_trace or args.ledger_user_trace:
            from buywait.domain.enums import LifecycleDisposition
            from buywait.evidence.service import EvidenceService
            from buywait.ledger import CanonicalLedgerBuilder, redacted_ledger_trace
            from buywait.providers.local_ocr import local_ocr
            if args.ledger_trace and args.ledger_user_trace:
                print(json.dumps({"code": "conflicting_trace_targets"}), file=sys.stderr)
                return 2
            request_id = args.ledger_trace
            if args.ledger_user_trace:
                requests = context.requests_by_user_id.get(args.ledger_user_trace, ())
                if len(requests) != 1:
                    print(json.dumps({"code": "user_request_not_unique", "user_id": args.ledger_user_trace,
                                      "request_count": len(requests)}), file=sys.stderr)
                    return 2
                request_id = requests[0].request_id
            if request_id not in context.requests_by_id:
                print(json.dumps({"code": "unknown_request_id"}), file=sys.stderr)
                return 2
            service = EvidenceService(context, ocr=local_ocr(args.ocr), model=None, cache=None)
            evidence = service.evaluate(request_id)
            ledger = CanonicalLedgerBuilder().build(context.bundle(request_id), evidence)
            print(json.dumps(redacted_ledger_trace(ledger), sort_keys=True, indent=2))
            return 3 if any(item.disposition is LifecycleDisposition.UNRESOLVED
                            for item in ledger.event_dispositions) else 0
        if args.evidence_preflight or args.evidence_trace:
            from buywait.domain.errors import BuyWaitError
            from buywait.evidence.cache import EvidenceCache
            from buywait.evidence.service import EvidenceService
            from buywait.providers.local_ocr import local_ocr
            entry_dir = Path(__file__).resolve().parent
            root = entry_dir.parent if entry_dir.name == "code" else entry_dir
            cache_dir = Path(args.evidence_cache_dir or ".cache/evidence")
            if not cache_dir.is_absolute():
                cache_dir = root / cache_dir
            try:
                if cache_dir.resolve().is_relative_to(context.dataset_dir):
                    raise BuyWaitError("Generated evidence cache must be outside the read-only dataset.", code="unsafe_artifact_path")
                model = None
                if args.evidence_model == "openai":
                    from buywait.providers.openai_evidence import OpenAIEvidenceProvider
                    from buywait.providers.usage import JsonlUsageSink
                    usage_path = Path(args.evidence_usage_log or "audit/evidence-usage.jsonl")
                    if not usage_path.is_absolute():
                        usage_path = root / usage_path
                    if usage_path.resolve().is_relative_to(context.dataset_dir):
                        raise BuyWaitError("Usage logs must be outside the read-only dataset.", code="unsafe_artifact_path")
                    model = OpenAIEvidenceProvider.from_environment(usage_sink=JsonlUsageSink(usage_path))
                service = EvidenceService(context, ocr=local_ocr(args.ocr), model=model,
                                          cache=None if args.no_evidence_cache else EvidenceCache(cache_dir))
                if args.evidence_trace:
                    if args.evidence_trace not in context.requests_by_id:
                        print(json.dumps({"code": "unknown_request_id"}), file=sys.stderr)
                        return 2
                    result = service.evaluate(args.evidence_trace)
                    report, ready = result.redacted(), result.ready
                else:
                    report = service.preflight()
                    ready = not report["unresolved"]
                report["providers"] = {"ocr": service.ocr.provider_id if service.ocr else None,
                                       "ocr_version": service.ocr.version if service.ocr else None,
                                       "model": model.model if model else None, "network_enabled": model is not None}
                print(json.dumps(report, sort_keys=True, indent=2))
                return 0 if ready else 3
            except BuyWaitError as error:
                print(json.dumps(error.as_dict(), sort_keys=True), file=sys.stderr)
                return 3
        summary = context.summary()
        if args.profile_inputs:
            print(json.dumps(summary, sort_keys=True, indent=2))
        else:
            print(json.dumps({"valid": True, "counts": summary["counts"], "warnings": summary["warnings"]}, sort_keys=True))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Clean-package prediction, independent CSV replay, and public-only scorecard."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buywait.adapters.dataset import load_dataset
from buywait.application import BuyWaitDecisionService
from buywait.evidence.service import EvidenceService
from buywait.planning import read_output, write_output_atomic
from buywait.providers.local_ocr import local_ocr
from buywait.forecasting.policy import RELEASE_CANDIDATE_POLICY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    start = time.monotonic()
    started = datetime.now(timezone.utc).isoformat()
    context = load_dataset(args.dataset_dir)
    ocr = local_ocr('rapidocr')
    service = BuyWaitDecisionService(context, evidence_service=EvidenceService(
        context, ocr=ocr, model=None, cache=None))
    results = []
    for index, request in enumerate(context.evaluation_requests, 1):
        result = service.decide(request.request_id)
        if result.assessment.timeline.unresolved_event_ids:
            raise RuntimeError('Unresolved mandatory evidence: ' + request.request_id)
        results.append(result)
        if index % 50 == 0:
            print('Reproduced', index, 'evaluation requests', flush=True)
    target = Path(args.output).resolve()
    write_output_atomic(target, [r.decision for r in results], dataset_dir=context.dataset_dir)
    # Parse actual disk bytes, then replay each serialized plan independently.
    restored = read_output(target, context)
    for decision, result in zip(restored, results, strict=True):
        service.validator.require_valid(decision, context.bundle(decision.request_id), result.assessment)
    evaluation_elapsed = time.monotonic() - start
    fields = ('amount_safe_to_pay', 'affordability_status', 'recommended_payment_method',
              'payment_plan', 'earliest_date_for_full_payment', 'spending_changes_needed')
    public = Counter()
    for request in context.sample_requests:
        decision = service.decide(request.request_id).decision
        expected = context.sample_expectations[request.request_id]
        for field in fields:
            public[field] += getattr(decision, field) == getattr(expected, field)
    report = dict(passed=True, rows=len(restored), validation_errors=[],
        public_cases=len(context.sample_requests), public_exact=dict(public),
        methods=dict(Counter(r.decision.recommended_payment_method.value for r in results)),
        unresolved=0, model_calls=0, input_tokens=0, output_tokens=0, estimated_cost_usd='0',
        cache_policy='disabled', ocr_provider=ocr.provider_id, ocr_version=ocr.version,
        policy_fingerprint=RELEASE_CANDIDATE_POLICY.fingerprint, started_utc=started,
        evaluation_seconds=evaluation_elapsed, total_seconds=time.monotonic()-start,
        output_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()

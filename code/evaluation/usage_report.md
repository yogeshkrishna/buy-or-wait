# Final prediction-run token usage and cost

Authoritative run: 2026-09-13, Windows 11, Python 3.12.14.

Command: `python code/main.py --generate-output --ocr rapidocr --output-csv output.csv` (from the repository root; clean installations supply `--dataset-dir /absolute/path/to/dataset`).

The completed run produced 250 evaluation predictions, zero unresolved mandatory-evidence requests and no reported generation errors. Public examples were not used to choose production predictions. Development API failures and public-calibration runs are excluded from these totals.

| Provider/model | Calls | Input tokens | Output tokens | Total tokens | Estimated cost USD |
|---|---:|---:|---:|---:|---:|
| Remote LLM/VLM: none | 0 | 0 | 0 | 0 | 0 |
| Overall | 0 | 0 | 0 | 0 | 0 |

Average per evaluation request: 0 input tokens, 0 output tokens, 0 total tokens, USD 0 external model cost. There are no unknown remote-usage calls in this run. Pricing source/date: not applicable because no paid model/provider was invoked. These figures exclude electricity, hardware and development-assistant usage; local CPU inference is not a token-billed API.

Evidence configuration: deterministic multilingual parsing plus `rapidocr-onnxruntime==1.4.4`, ONNX Runtime 1.30.0, NumPy 2.5.3, Pillow 12.3.0. OCR's bundled local detection/recognition/classification neural networks run on CPU; they do not report LLM input/output tokens. The CLI explicitly constructs evidence with `model=None, cache=None`: no remote calls and no disk-cache hits. Local OCR invocation counts were not instrumented by the production CLI and are not invented here.

Frozen policy fingerprint: `8edca989bb4e50d043c65aced10f35f98668fb7f8d30d439a2ee52e53adaa03f`.
Run identifier: `buywait-release-20260913-offline-final`. The original prediction artifact is [output.csv](../../output.csv), SHA-256 `f1ef671aa68c400471c2c05bf9f35ad38c5ba50d969f9fc91e19a07dcc4c9c2c`. Public-example metrics and reproduction instructions are in the [project README](../../README.md); those separate checks do not add paid-model usage to the authoritative run.

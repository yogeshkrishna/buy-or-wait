"""Private local OCR worker. stdin is bounded PNG bytes; stdout is JSON only."""
import json
import sys
from rapidocr_onnxruntime import RapidOCR

if __name__ == "__main__":
    data = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise SystemExit(2)
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2, det_limit_side_len=1600, det_limit_type="max", text_score=0.6)
    result, elapsed = engine(data)
    print(json.dumps({"result": result or []}, ensure_ascii=True))

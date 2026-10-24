"""Append-only, credential-free model-call accounting; independent of cache writes."""
from dataclasses import asdict
import json
from pathlib import Path


class JsonlUsageSink:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def record(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(asdict(record), sort_keys=True, default=str) + "\n")

#!/usr/bin/env python3
"""Measure synthetic catalog scans without reading a real Codex account.

Use --module with a saved older codex_app_server.py to compare implementations.
Times describe this local process, not mobile or end-to-end performance.
"""

import argparse
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


class CountedFile:
    def __init__(self, handle, counters):
        self.handle, self.counters = handle, counters

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def read(self, *args):
        result = self.handle.read(*args)
        self.counters["bytesRead"] += len(result)
        return result

    def readline(self, *args):
        result = self.handle.readline(*args)
        self.counters["bytesRead"] += len(result)
        return result


def benchmark(module_path):
    spec = importlib.util.spec_from_file_location("pocket_cache_subject", module_path)
    subject = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = subject
    spec.loader.exec_module(subject)
    row = json.dumps({"type": "response_item", "payload": {
        "type": "message", "text": "x" * 1024,
    }}) + "\n"
    results = []
    with tempfile.TemporaryDirectory(prefix="pocket-cache-benchmark-") as directory:
        root = Path(directory)
        entries = []
        for index in range(50):
            path = root / f"rollout-t{index}.jsonl"
            path.write_text(row * 480, encoding="utf-8")
            entries.append({"id": f"t{index}", "path": str(path)})
        file_bytes = Path(entries[0]["path"]).stat().st_size
        original_open = Path.open
        for count in (8, 9, 50):
            subject._ROLLOUT_ACTIVITY_CACHE.clear()
            counters = {"fileOpens": 0, "bytesRead": 0}

            def counted_open(path, *args, **kwargs):
                handle = original_open(path, *args, **kwargs)
                if path.parent == root and args and args[0] == "rb":
                    counters["fileOpens"] += 1
                    return CountedFile(handle, counters)
                return handle

            passes = []
            with patch.object(Path, "open", counted_open):
                for index in range(3):
                    before = counters.copy()
                    start = time.perf_counter()
                    for thread in entries[:count]:
                        subject.summarize_thread(thread)
                    passes.append({
                        "pass": index + 1,
                        "fileOpens": counters["fileOpens"] - before["fileOpens"],
                        "bytesRead": counters["bytesRead"] - before["bytesRead"],
                        "elapsedMs": round((time.perf_counter() - start) * 1000, 3),
                    })
            results.append({"tasks": count, "bytesPerFile": file_bytes, "passes": passes})
    return {"scope": "Synthetic files, local process only; no model, UI, or network.",
            "catalogScans": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path,
                        default=Path(__file__).resolve().parents[1] / "codex_app_server.py")
    args = parser.parse_args()
    print(json.dumps(benchmark(args.module), indent=2))

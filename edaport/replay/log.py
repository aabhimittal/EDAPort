"""Append-only JSONL replay log. One line per tool call; run ids are sequential (r-0001, ...)."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path


class ReplayLog:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, record: dict) -> str:
        """Assign the next run_id under an exclusive lock and append. Returns the run_id."""
        with open(self.path, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            n = sum(1 for _ in f)
            run_id = f"r-{n + 1:04d}"
            rec = {"run_id": run_id, "t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **record}
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return run_id

    def records(self) -> list[dict]:
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def get(self, run_id: str) -> dict:
        for rec in self.records():
            if rec["run_id"] == run_id:
                return rec
        raise KeyError(f"unknown run_id {run_id!r}")

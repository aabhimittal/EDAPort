"""Append-only JSONL replay log. One line per tool call; run ids are sequential (r-0001, ...)."""

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


class ReplayLog:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._offsets = [0]  # byte offset where line i+1 starts; the log is append-only, so these never move

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
        """Run ids are line numbers (r-0007 = line 7), so a lookup seeks instead of parsing the whole log."""
        m = re.fullmatch(r"r-(\d+)", run_id)
        n = int(m.group(1)) if m else 0
        with open(self.path, "rb") as f:
            f.seek(self._offsets[-1])
            while len(self._offsets) <= n and f.readline().endswith(b"\n"):  # index complete lines only
                self._offsets.append(f.tell())
            if 1 <= n < len(self._offsets):
                f.seek(self._offsets[n - 1])
                rec = json.loads(f.readline())
                if rec["run_id"] == run_id:
                    return rec
        raise KeyError(f"unknown run_id {run_id!r}")

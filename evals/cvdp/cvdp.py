"""CVDP on EDAPort: the open-tool agentic subset (spec section 9a), frozen by hash.

The dataset is not redistributed here. `fetch()` downloads the one JSONL file named in manifest.json and
refuses it unless its sha256 matches. Each task becomes an EDAPort snapshot (`import_task`); a candidate
design_ref is scored by running the task's own harness (docker compose + cocotb + pytest) unchanged.

Scoring is the eval's job, not an agent tool: the harness holds the hidden tests.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "manifest.json").read_text())
CACHE = Path(os.environ.get("EDAPORT_CVDP_CACHE", HERE / ".cache"))
CATEGORY = {"cid003": "spec to RTL", "cid004": "RTL modification", "cid005": "spec to RTL, module reuse",
            "cid016": "debugging / bug fix"}


def fetch() -> Path:
    path = CACHE / MANIFEST["file"]
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(MANIFEST["url"], timeout=120) as r:  # noqa: S310 (fixed https URL)
            data = r.read()
        path.write_bytes(data)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != MANIFEST["sha256"]:
        raise RuntimeError(f"{path} has sha256 {digest}, manifest pins {MANIFEST['sha256']}: not the frozen task set")
    return path


def load() -> dict[str, dict]:
    tasks = {}
    for line in fetch().read_text().splitlines():
        t = json.loads(line)
        tasks[t["id"]] = t
    assert sorted(tasks) == sorted(x["id"] for x in MANIFEST["tasks"]), "task ids differ from the manifest"
    return tasks


def import_task(engine, task: dict, solution: dict[str, str] | None = None) -> str:
    """Task context (+ optional solution files) -> design_ref."""
    files = {**task["context"], **(solution or {})}
    blobs = {path: engine.store.put_blob(text.encode()) for path, text in files.items()}
    return engine.store.put_manifest(blobs, {"cvdp_task": task["id"]})


def score(engine, design_ref: str, task: dict, image: str, timeout_s: float = 900) -> dict:
    """Run the task's harness on design_ref. passed = every compose service exits 0."""
    with tempfile.TemporaryDirectory(prefix="cvdp-") as tmp:
        tmp = Path(tmp)
        for rel, text in task["harness"].items():
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text.replace("__OSS_SIM_IMAGE__", image))
        code = tmp / "code"
        engine.store.materialize(design_ref, code)
        for d in ("rundir", "rundir/harness", "rtl", "verif", "docs"):
            (code / d).mkdir(parents=True, exist_ok=True)
        compose = ["docker", "compose", "-f", str(tmp / "docker-compose.yml"), "-p", f"cvdp{os.getpid()}"]
        services = subprocess.run(compose + ["config", "--services"], capture_output=True, text=True).stdout.split()
        result = {"task": task["id"], "passed": bool(services), "services": {}, "detail": ""}
        if "Dockerfile" in task["harness"]:
            b = subprocess.run(compose + ["build"], capture_output=True, text=True, timeout=timeout_s)
            if b.returncode != 0:
                return {**result, "passed": False, "detail": "harness image build failed: " + _tail(b.stderr)}
        for svc in services:
            try:
                r = subprocess.run(compose + ["run", "--rm", "-v", f"{code}:/code", "-v", f"{code}/rundir:/rundir", svc],
                                   capture_output=True, text=True, timeout=timeout_s)
                rc, log = r.returncode, r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                rc, log = None, "timeout"
            result["services"][svc] = rc
            if rc != 0:
                result["passed"] = False
                result["detail"] = result["detail"] or _why(log)
        subprocess.run(compose + ["down", "--remove-orphans"], capture_output=True)
        return result


def _tail(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


def _why(log: str) -> str:
    """One line saying why the harness failed: compile error, failed test, or (worst case) infrastructure."""
    for pat, label in ((r"^.*(?:No such file or directory|Unable to open input file).*$", "missing file"),
                       (r"^.*(?:syntax error|error:).*$", "compile error"),
                       (r"^.*(?:AssertionError|assert ).*$", "test assertion"),
                       (r"^.*\bFAIL(?:ED)?\b.*$", "test failed")):
        m = re.search(pat, log, re.M)
        if m:
            return f"{label}: {m.group(0).strip()[:160]}"
    return "other: " + _tail(log)

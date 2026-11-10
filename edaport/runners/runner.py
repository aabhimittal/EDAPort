"""Tool execution: local subprocess or pinned OCI image. Both return the same RunOutput."""

import hashlib
import json
import os
import re
import resource
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

LOCK_FILE = Path(__file__).resolve().parents[2] / "images" / "images.lock.json"
CONTAINER_WORKDIR = "/work"


@dataclass
class RunOutput:
    rc: int | None
    log: str
    wall_s: float
    cpu_s: float | None
    timed_out: bool = False
    missing_tool: str | None = None


def load_lock(path: Path = LOCK_FILE) -> dict:
    return json.loads(path.read_text())


class LocalRunner:
    """Runs host binaries. image_digest is a hash of the tool's version banner (no container to pin)."""

    kind = "local"

    def __init__(self, mem_limit_mb: int | None = 8192):
        self.mem_limit_mb = mem_limit_mb

    def identity(self, binary: str) -> str:
        return _local_identity(binary)

    def run(self, argv: list[str], cwd: Path, timeout_s: float, env: dict | None = None) -> RunOutput:
        mem = self.mem_limit_mb

        def limits():
            os.setsid()  # own process group so a timeout kills the whole tree
            if mem:
                resource.setrlimit(resource.RLIMIT_AS, (mem << 20, mem << 20))

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env={**os.environ, **(env or {})}, preexec_fn=limits)
        except FileNotFoundError:
            return RunOutput(None, f"tool not found: {argv[0]}", 0.0, 0.0, missing_tool=argv[0])
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        return RunOutput(proc.returncode, out.decode(errors="replace"), round(time.monotonic() - t0, 3),
                         round(cpu, 3), timed_out)


@lru_cache(maxsize=None)
def _local_identity(binary: str) -> str:
    for flag in ("--version", "-V"):
        try:
            out = subprocess.run([binary, flag], capture_output=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        banner = (out.stdout or out.stderr).decode(errors="replace").strip().splitlines()
        if out.returncode == 0 and banner:
            return "local:sha256:" + hashlib.sha256(banner[0].encode()).hexdigest()[:16] + f" ({banner[0][:80]})"
    return f"local:unknown:{binary}"


class DockerRunner:
    """Runs inside a digest-pinned image with no network; the workdir is bind-mounted at /work."""

    kind = "docker"

    def __init__(self, image: str, docker: str = "docker", cpus: float | None = None, mem_limit_mb: int | None = None):
        if "@sha256:" not in image and not re.match(r"^sha256:[0-9a-f]{64}$", image):
            raise ValueError(f"image must be pinned by digest (repo@sha256:... or a local sha256: image id): {image}")
        self.image, self.docker, self.cpus, self.mem_limit_mb = image, docker, cpus, mem_limit_mb

    def identity(self, binary: str) -> str:
        return self.image

    def run(self, argv: list[str], cwd: Path, timeout_s: float, env: dict | None = None) -> RunOutput:
        # `times` reports child CPU of the wrapped command, giving cpu_s without cgroup plumbing.
        script = shlex.join(argv) + '; rc=$?; echo "@@EDAPORT_TIMES"; times; exit $rc'
        cmd = [self.docker, "run", "--rm", "--network=none", "-v", f"{Path(cwd).resolve()}:{CONTAINER_WORKDIR}",
               "-w", CONTAINER_WORKDIR, "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        if self.cpus:
            cmd += ["--cpus", str(self.cpus)]
        if self.mem_limit_mb:
            cmd += ["--memory", f"{self.mem_limit_mb}m"]
        name = f"edaport-{os.getpid()}-{time.monotonic_ns()}"
        cmd += ["--name", name, self.image, "bash", "-c", script]
        out = LocalRunner(mem_limit_mb=None).run(cmd, cwd, timeout_s)
        if out.timed_out:
            subprocess.run([self.docker, "kill", name], capture_output=True)
        if out.missing_tool:
            return out
        log, cpu = _split_times(out.log)
        return RunOutput(out.rc, log, out.wall_s, cpu, out.timed_out)


def _split_times(log: str) -> tuple[str, float | None]:
    head, sep, tail = log.rpartition("@@EDAPORT_TIMES\n")
    if not sep:
        return log, None
    lines = tail.strip().splitlines()
    if len(lines) < 2:
        return head, None
    # second line of `times` = children user/sys, e.g. "1m2.500s 0m0.300s"
    total = 0.0
    for tok in lines[1].split():
        m, s = tok.rstrip("s").split("m")
        total += int(m) * 60 + float(s)
    return head, round(total, 3)

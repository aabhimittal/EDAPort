"""M3: the LLM A/B eval. Same model, same task prompt; only the tool interface differs.

  A0  one `shell` tool: bash in the pinned oss-tools container with the design mounted (raw logs + shell)
  A1  the EDAPort MCP tools

Per task and arm it records the spec's metrics: solved, tokens, tool calls, tool-call errors, wall time.
Needs credentials for the Anthropic API (`pip install edaport[eval]`); without them use --dry-run, which
lists the tasks and tool surfaces. The loop itself is tested with a scripted stand-in model
(tests/test_llm_agent.py). It has NOT been run against a real model yet.

    python evals/ab_interface/llm_agent.py --arms A0,A1 --tasks counter_repair [--model ...] [--effort ...]
    python evals/ab_interface/llm_agent.py --suite cvdp --limit 10
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import anyio

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "evals/cvdp")]
from edaport.runners.runner import load_lock  # noqa: E402
from edaport.server.engine import Engine  # noqa: E402
from edaport.server.mcp_server import build_server  # noqa: E402

MODEL = "claude-opus-5-5"
SOLUTION = "solution"  # A1: the branch the agent must leave its final design on
SHELL_OUTPUT_CAP = 200_000  # chars; a larger dump would not fit the context anyway, and the cut is stated
SYSTEM = {
    "A0": ("You are an RTL engineer. The design is in /work inside a container with yosys, iverilog, verilator, "
           "sby and cocotb. Use the `shell` tool to inspect, edit and test it. Your final answer is the state of "
           "the files in /work when you stop. Stop when the task is done."),
    "A1": ("You are an RTL engineer. Work through the EDAPort tools: the design is the snapshot on branch `main`. "
           "Read files with snapshot_read, change them with snapshot_write, check with the lint/sim/formal/synth "
           f"tools. Your final answer is whatever branch `{SOLUTION}` points at when you stop: pass "
           f"branch=\"{SOLUTION}\" to snapshot_write (or use snapshot_branch). Stop when the task is done."),
}


@dataclass
class Task:
    id: str
    prompt: str
    setup: Callable[[Engine], str]          # -> design_ref of the starting design
    check: Callable[[Engine, str], bool]    # design_ref of the candidate -> solved?


@dataclass
class Result:
    task: str
    arm: str
    solved: bool = False
    stop: str = ""
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_s: float = 0.0


# ---------------------------------------------------------------- the agent loop (arm-independent)
def run_agent(client, model: str, effort: str, system: str, prompt: str, tools: list[dict],
              execute: Callable[[str, dict], tuple[str, bool]], res: Result, max_turns: int = 60,
              token_limit: int = 2_000_000) -> None:
    """Manual tool loop (not the SDK tool runner): the eval needs per-turn token and error accounting."""
    messages = [{"role": "user", "content": prompt}]
    while res.turns < max_turns and res.input_tokens + res.output_tokens < token_limit:
        response = client.beta.messages.create(
            model=model, max_tokens=16000, system=system, tools=tools, messages=messages,
            output_config={"effort": effort}, cache_control={"type": "ephemeral"},
            # On a safety decline the API re-runs the request on a fallback model instead of stopping the task.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        res.turns += 1
        u = response.usage
        res.input_tokens += (u.input_tokens + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                             + (getattr(u, "cache_read_input_tokens", 0) or 0))
        res.output_tokens += u.output_tokens
        messages.append({"role": "assistant", "content": response.content})  # thinking blocks go back unchanged
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            res.stop = response.stop_reason  # end_turn, max_tokens or refusal
            return
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            res.tool_calls += 1
            text, is_error = execute(block.name, block.input)
            res.tool_errors += is_error
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})  # all results in one message
    res.stop = "turn_limit" if res.turns >= max_turns else "token_limit"


# ---------------------------------------------------------------- arm A1: EDAPort tools
def edaport_tools(engine: Engine) -> list[dict]:
    async def listing():
        return await build_server(engine).list_tools()
    tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
             for t in anyio.run(listing)]
    return [t for t in tools if t["name"] != "snapshot_import"]  # the harness imports; agents do not read the host


def run_a1(client, model, effort, task: Task, engine: Engine, res: Result) -> str | None:
    start = task.setup(engine)
    engine.store.branch(start, "main")
    engine.store.branch(start, SOLUTION)

    def execute(name: str, args: dict) -> tuple[str, bool]:
        try:
            out = engine.call(name, **args)
        except (TypeError, ValueError) as e:  # unknown tool or wrong argument names
            return f"invalid call: {e}", True
        return json.dumps(out), not out["ok"]

    run_agent(client, model, effort, SYSTEM["A1"], task.prompt, edaport_tools(engine), execute, res)
    return engine.store.resolve(SOLUTION)


# ---------------------------------------------------------------- arm A0: raw shell in a container
SHELL_TOOL = {"name": "shell", "description": "Run a bash command in /work (the design directory) and return "
              "its combined stdout and stderr and exit code. No network.",
              "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                               "required": ["command"], "additionalProperties": False}}


def run_a0(client, model, effort, task: Task, engine: Engine, res: Result, image: str,
           timeout_s: float = 600) -> str | None:
    work = Path(tempfile.mkdtemp(prefix="edaport-a0-"))
    try:
        engine.store.materialize(task.setup(engine), work)

        def execute(name: str, args: dict) -> tuple[str, bool]:
            if name != "shell" or not isinstance(args.get("command"), str):
                return "invalid call: the only tool is shell(command)", True
            try:
                p = subprocess.run(["docker", "run", "--rm", "--network=none", "-v", f"{work}:/work", "-w", "/work",
                                    image, "bash", "-c", args["command"]],
                                   capture_output=True, text=True, timeout=timeout_s, errors="replace")
            except subprocess.TimeoutExpired:
                return f"timed out after {timeout_s}s", True
            out = p.stdout + p.stderr
            if len(out) > SHELL_OUTPUT_CAP:
                out = out[:SHELL_OUTPUT_CAP] + f"\n[output cut at {SHELL_OUTPUT_CAP} of {len(out)} chars]"
            return f"{out}\n[exit code {p.returncode}]", p.returncode != 0

        run_agent(client, model, effort, SYSTEM["A0"], task.prompt, [SHELL_TOOL], execute, res)
        return engine.store.import_dir(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- tasks
def counter_repair() -> Task:
    def check(e: Engine, ref: str) -> bool:
        # Hidden checks: the original testbench with unseen seeds, and the formal property as shipped.
        tb, sby = (ROOT / "examples/counter/tb/tb_counter.sv").read_text(), (ROOT / "examples/counter/formal/counter.sby").read_text()
        ref = e.store.write_file(e.store.write_file(ref, "tb/tb_counter.sv", tb.encode()), "formal/counter.sby", sby.encode())
        sims = [e.call("sim_run", design_ref=ref, tb_ref="tb/tb_counter.sv", seed=s)["status"] for s in (11, 12, 13)]
        proof = e.call("formal_run", design_ref=ref, sby_ref="formal/counter.sby", mode="prove", depth=12)["status"]
        return sims == ["pass"] * 3 and proof == "pass"

    return Task("counter_repair",
                "The counter in rtl/counter.v must count 0..MAX and wrap to 0, but simulation with "
                "tb/tb_counter.sv shows it reaching MAX+1. Find and fix the bug in the RTL. Do not change the "
                "testbench or the FORMAL block.",
                lambda e: e.store.import_dir(ROOT / "examples/counter"), check)


def cvdp_tasks(limit: int | None) -> list[Task]:
    import cvdp
    image = load_lock()["oss-tools"]["image"]
    out = []
    for tid, t in sorted(cvdp.load().items())[:limit]:
        out.append(Task(tid, t["prompt"].replace("/code/", ""), lambda e, t=t: cvdp.import_task(e, t),
                        lambda e, ref, t=t: cvdp.score(e, ref, t, image)["passed"]))
    return out


# ---------------------------------------------------------------- driver
def run(client, tasks: list[Task], arms: list[str], model: str, effort: str, home: str, image: str) -> list[Result]:
    results = []
    for task in tasks:
        for arm in arms:
            engine = Engine(Path(home) / f"{task.id}-{arm}")  # separate state per run: arms must not share work
            res, t0 = Result(task.id, arm), time.time()
            ref = (run_a1(client, model, effort, task, engine, res) if arm == "A1"
                   else run_a0(client, model, effort, task, engine, res, image))
            res.wall_s = round(time.time() - t0, 1)
            res.solved = bool(ref) and res.stop != "refusal" and task.check(engine, ref)
            results.append(res)
            print(f"{task.id} {arm}: solved={res.solved} stop={res.stop} turns={res.turns} "
                  f"tokens={res.input_tokens + res.output_tokens:,} errors={res.tool_errors}/{res.tool_calls}", flush=True)
    return results


def table(results: list[Result]) -> str:
    lines = ["| arm | tasks | solve rate | tokens per solved task | tool-call error rate | wall per solve (min) |",
             "|---|---:|---:|---:|---:|---:|"]
    for arm in sorted({r.arm for r in results}):
        rs = [r for r in results if r.arm == arm]
        n = sum(r.solved for r in rs)
        calls, errs = sum(r.tool_calls for r in rs), sum(r.tool_errors for r in rs)
        tokens = sum(r.input_tokens + r.output_tokens for r in rs)  # all tokens spent, charged to the solves
        per_solve = f"{tokens / n:,.0f}" if n else "-"
        err_rate = f"{errs}/{calls} ({errs / calls:.1%})" if calls else "-"
        wall = f"{sum(r.wall_s for r in rs) / n / 60:.1f}" if n else "-"
        lines.append(f"| {arm} | {len(rs)} | {n}/{len(rs)} | {per_solve} | {err_rate} | {wall} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A0,A1")
    ap.add_argument("--suite", choices=["counter", "cvdp"], default="counter")
    ap.add_argument("--limit", type=int, help="first N tasks of the suite")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--home", default=str(ROOT / ".edaport-llm-eval"))
    ap.add_argument("--image", default=load_lock()["oss-tools"]["image"])
    ap.add_argument("--out", default=str(ROOT / "evals/ab_interface/llm_results.md"))
    ap.add_argument("--dry-run", action="store_true", help="no API calls: list tasks and tool surfaces")
    a = ap.parse_args()
    tasks = [counter_repair()] if a.suite == "counter" else cvdp_tasks(a.limit)
    if a.dry_run:
        names = [t["name"] for t in edaport_tools(Engine(tempfile.mkdtemp(prefix="edaport-dry-")))]
        print(f"{len(tasks)} tasks: {[t.id for t in tasks][:5]}...\nA0 tools: ['shell']\nA1 tools ({len(names)}): {names}")
        return 0
    import anthropic
    results = run(anthropic.Anthropic(), tasks, a.arms.split(","), a.model, a.effort, a.home, a.image)
    text = (f"# LLM A/B interface eval\n\nModel `{a.model}`, effort `{a.effort}`, suite `{a.suite}`, "
            f"{len(tasks)} tasks.\n\n" + table(results) + "\n")
    Path(a.out).write_text(text)
    Path(a.out).with_suffix(".json").write_text(json.dumps([r.__dict__ for r in results], indent=1) + "\n")
    print("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

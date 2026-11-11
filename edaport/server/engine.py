"""Tool implementations behind the MCP surface.

Every call goes through one pipeline:
  resolve design_ref -> materialize into a scratch dir -> run pinned tool(s) -> parse to a typed payload
  -> capture declared outputs as a new snapshot -> store raw log + full result -> append replay record
  -> bound to the token budget -> validate against the result schema.
"""

import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from ..models import RESULT_MODELS
from ..parsers import (iverilog_lint, junit_xml, klayout_lyrdb, netgen_lvs, opensta_json, orfs_metrics, sby_status, sim_log,
                       vcd, verilator_coverage, verilator_lint, yosys_equiv, yosys_synth)
from ..replay.log import ReplayLog
from ..runners.runner import CONTAINER_WORKDIR, DockerRunner, LocalRunner, load_lock
from ..snapshot.store import Store, StoreError, check_relpath
from ..util import canonical, sha256
from .budget import bound, get_path

COV_MAIN = Path(__file__).resolve().parents[1] / "runners" / "verilator_cov_main.cpp"
DEFAULT_BUDGET = int(os.environ.get("EDAPORT_BUDGET_TOKENS", "2000"))
HDL = (".v", ".sv")
TOOL_OWNED = ("build/", "orfs/")  # write scope: only tools write here, agents cannot
MAX_WRITE_BYTES = 1 << 20
VOLATILE = {"run_id", "design_ref", "truncated", "more_handle", "omitted"}
ORFS_STAGES = ["synth", "floorplan", "place", "cts", "grt", "route", "finish"]
# ORFS step-file prefixes per stage, used to (a) clear a stage's stale outputs and (b) pick its metrics.
ORFS_PREFIX = {"synth": ("1_",), "floorplan": ("2_",), "place": ("3_",), "cts": ("4_",), "grt": ("5_1_",),
               "route": ("5_2_", "5_3_", "5_route"), "finish": ("6_",)}
ORFS_ODB = [("finish", "6_final"), ("route", "5_route"), ("grt", "5_1_grt"), ("cts", "4_cts"),
            ("place", "3_place"), ("floorplan", "2_floorplan"), ("synth", "1_synth")]
_OVERRIDE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_OVERRIDE_VAL = re.compile(r"^[A-Za-z0-9_.,/:+ -]{0,256}$")
_ORFS_ERROR = re.compile(r"\[ERROR [A-Z]+-\d+\].*|^Error: .*|make: \*\*\* .*", re.M)


class ToolFail(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind, self.msg = kind, msg


class Ctx:
    """Per-call execution context: scratch dir, accumulated raw log, resource usage, outputs to capture."""

    def __init__(self, engine: "Engine", workdir: Path, payload: dict):
        self.engine, self.wd, self.payload = engine, workdir, payload
        self.logs: list[str] = []
        self.wall = 0.0
        self.cpu: float | None = 0.0
        self.images: dict[str, str] = {}
        self.capture: list[str] = []
        self.meta: dict = {}
        self.seed = None
        self.threads = None
        self.after_capture = None  # payload fields that name the new snapshot (e.g. netlist_ref)

    def run(self, argv: list[str], timeout_s: float, runner=None, env=None, cwd: Path | None = None):
        runner = runner or self.engine.oss
        if runner.kind == "docker" and cwd and Path(cwd) != self.wd:
            # mount the whole scratch dir so relative paths like ../rtl stay visible; cd inside the container
            argv = ["bash", "-c", f"cd {shlex.quote(str(Path(cwd).relative_to(self.wd)))} && exec {shlex.join(argv)}"]
            cwd = None
        out = runner.run(argv, cwd or self.wd, timeout_s, env)
        # Scratch paths differ per run; normalize so logs and parsed messages are reproducible.
        log = out.log.replace(str(self.wd.resolve()), ".").replace(str(self.wd), ".")
        if runner.kind == "docker":
            log = log.replace(CONTAINER_WORKDIR + "/", "./")
        out.log = log
        self.logs.append(f"$ {shlex.join(argv)}\n{log}")
        self.wall += out.wall_s
        self.cpu = None if out.cpu_s is None or self.cpu is None else self.cpu + out.cpu_s
        self.images[argv[0]] = runner.identity(argv[0])
        if out.missing_tool:
            raise ToolFail("tool_missing", f"{out.missing_tool} is not installed (set EDAPORT_RUNNER=docker?)")
        if out.timed_out:
            raise ToolFail("timeout", f"{argv[0]} exceeded {timeout_s}s")
        return out

    def blob(self, rel: str) -> str | None:
        path = self.wd / rel
        return self.engine.store.put_blob(path.read_bytes()) if path.is_file() else None


class Engine:
    def __init__(self, home: str | os.PathLike | None = None, runner: str | None = None):
        self.home = Path(home or os.environ.get("EDAPORT_HOME", ".edaport")).resolve()
        self.store = Store(self.home / "store")
        self.log = ReplayLog(self.home / "runs.jsonl")
        self.lock = load_lock()
        mode = runner or os.environ.get("EDAPORT_RUNNER", "local")
        # local: host binaries, NOT sandboxed (testbenches, DRC decks and .sby files are code). docker: sandboxed.
        oss_image = os.environ.get("EDAPORT_OSS_IMAGE", self.lock["oss-tools"]["image"])
        # Container resource caps (cgroups): EDAPORT_DOCKER_CPUS / EDAPORT_DOCKER_MEM_MB, unset = unlimited.
        self.limits = {"cpus": float(os.environ["EDAPORT_DOCKER_CPUS"]) if os.environ.get("EDAPORT_DOCKER_CPUS") else None,
                       "mem_limit_mb": int(os.environ["EDAPORT_DOCKER_MEM_MB"]) if os.environ.get("EDAPORT_DOCKER_MEM_MB") else None}
        self.oss = DockerRunner(oss_image, **self.limits) if mode == "docker" else LocalRunner()
        self._orfs = None
        roots = os.environ.get("EDAPORT_IMPORT_ROOTS", "")  # no default: the operator must opt directories in
        self.import_roots = [Path(r).resolve() for r in roots.split(os.pathsep) if r]

    @property
    def orfs(self) -> DockerRunner:
        if self._orfs is None:
            self._orfs = DockerRunner(os.environ.get("EDAPORT_ORFS_IMAGE", self.lock["orfs"]["image"]), **self.limits)
        return self._orfs

    # ------------------------------------------------------------------ dispatch
    def call(self, tool: str, /, **args) -> dict:
        fn = getattr(self, "t_" + tool, None)
        if fn is None or tool not in RESULT_MODELS:
            raise ValueError(f"unknown tool {tool!r}")
        budget = int(args.pop("budget_tokens", None) or DEFAULT_BUDGET)
        try:
            return fn(budget=budget, **args)
        except StoreError as e:
            return self._finish(tool, args, None, None, {}, None, budget, ToolFail("bad_input", str(e)))

    def _finish(self, tool, args, parent, new_ref, payload, ctx: Ctx | None, budget, fail: ToolFail | None) -> dict:
        model = RESULT_MODELS[tool]
        full = {"run_id": None, "tool": tool, "ok": fail is None,
                "error": {"kind": fail.kind, "msg": fail.msg[:2000]} if fail else None,
                "parent_ref": parent, "design_ref": new_ref, **payload}
        full = model.model_validate(full).model_dump(mode="json")  # schema check before anything is recorded
        digest = sha256(canonical({k: v for k, v in full.items() if k not in VOLATILE}))
        rec = {"tool": tool, "args": args, "parent_ref": parent, "output_ref": new_ref,
               "result_ref": self.store.put_blob(canonical(full)), "result_digest": digest,
               "ok": full["ok"]}
        if ctx is not None:
            rec.update(log_ref=self.store.put_blob("\n".join(ctx.logs).encode()),
                       image_digest=ctx.images, seed=ctx.seed, threads=ctx.threads,
                       wall_s=round(ctx.wall, 3), cpu_s=None if ctx.cpu is None else round(ctx.cpu, 3))
        full["run_id"] = self.log.append(rec)
        resp = bound(full, budget, more_handle=full["run_id"])
        return model.model_validate(resp).model_dump(mode="json")

    def _execute(self, tool, args, design_ref, payload, body, budget, ref_arg="design_ref"):
        """Run `body(ctx)` in a scratch dir materialized from design_ref; capture ctx.capture as a new snapshot."""
        ref = self.store.resolve(design_ref)
        if ref_arg:
            args = {**args, ref_arg: ref}  # log the resolved ref, never a movable label
        with tempfile.TemporaryDirectory(prefix="edaport-") as tmp:
            wd = Path(tmp)
            self.store.materialize(ref, wd)
            ctx = Ctx(self, wd, payload)
            fail = None
            try:
                body(ctx)
            except ToolFail as e:
                fail = e
            new_ref = ref
            if ctx.capture and fail is None:
                new_ref = self.store.capture(ref, wd, ctx.capture, ctx.meta)
                if ctx.after_capture:
                    ctx.after_capture(new_ref)
        return self._finish(tool, args, ref, new_ref, ctx.payload, ctx, budget, fail)

    def _record(self, tool, args, parent, new_ref, payload, budget):
        return self._finish(tool, args, parent, new_ref, payload, None, budget, None)

    # ------------------------------------------------------------------ helpers
    def _files(self, ref: str, files: list[str] | None, exclude_tb=True) -> list[str]:
        all_files = self.store.manifest(ref)["files"]
        if files:
            for f in files:
                if check_relpath(f) not in all_files:
                    raise ToolFail("bad_input", f"{f} not in snapshot")
            return files
        hdl = [f for f in all_files if f.endswith(HDL) and not f.startswith(TOOL_OWNED)]
        for root in ("rtl/", "src/"):
            picked = [f for f in hdl if f.startswith(root)]
            if picked:
                return picked
        skip = ("tb/", "test/", "tests/", "formal/", "sim/") if exclude_tb else ()
        picked = [f for f in hdl if not f.startswith(skip) and not Path(f).name.startswith("tb_")]
        if not picked:
            raise ToolFail("bad_input", "no HDL files found; pass files=[...]")
        return picked

    def _abs(self, ctx: Ctx, rel: str) -> str:
        """Absolute path as the tool sees it (verilator resolves user .cpp files from its -Mdir)."""
        return f"{CONTAINER_WORKDIR}/{rel}" if self.oss.kind == "docker" else str(ctx.wd / rel)

    @staticmethod
    def _need(ref_files: dict, path: str, what: str):
        if check_relpath(path) not in ref_files:
            raise ToolFail("bad_input", f"{what} {path!r} not in snapshot")

    # ------------------------------------------------------------------ lint / synth / sim
    def t_lint_run(self, design_ref: str, tool: str = "verilator", files: list[str] | None = None,
                   top: str | None = None, timeout_s: float = 300, budget=DEFAULT_BUDGET):
        args = {"tool": tool, "files": files, "top": top, "timeout_s": timeout_s}

        def body(ctx: Ctx):
            fl = self._files(self.store.resolve(design_ref), files)
            if tool == "verilator":
                argv = ["verilator", "--lint-only", "-Wall"] + (["--top-module", top] if top else []) + fl
                parsed = verilator_lint.parse(ctx.run(argv, timeout_s).log)
            elif tool == "iverilog":
                argv = ["iverilog", "-g2012", "-t", "null", "-Wall"] + (["-s", top] if top else []) + fl
                parsed = iverilog_lint.parse(ctx.run(argv, timeout_s).log)
            else:
                raise ToolFail("bad_input", f"lint tool must be verilator|iverilog, got {tool!r}")
            ctx.payload.update(parsed)

        return self._execute("lint_run", args, design_ref, {"tool_name": tool}, body, budget)

    def t_synth_run(self, design_ref: str, top: str, liberty_ref: str | None = None, files: list[str] | None = None,
                    timeout_s: float = 900, budget=DEFAULT_BUDGET):
        args = {"top": top, "liberty_ref": liberty_ref, "files": files, "timeout_s": timeout_s}
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", top):
            raise StoreError(f"invalid top module name {top!r}")

        def body(ctx: Ctx):
            ref = self.store.resolve(design_ref)
            fl = self._files(ref, files)
            out = f"build/synth/{top}.v"
            lib = ""
            if liberty_ref:
                self._need(self.store.manifest(ref)["files"], liberty_ref, "liberty_ref")
                lib = f" -liberty {liberty_ref}"
            script = [f"read_verilog -sv {' '.join(fl)}", f"synth -top {top}", "ltp -noff"]
            if liberty_ref:
                script += [f"dfflibmap{lib}", f"abc{lib}", "opt_clean"]
            script += [f"tee -q -o build/synth/stat.json stat -json{lib}", f"write_verilog -noattr {out}"]
            (ctx.wd / "build/synth").mkdir(parents=True, exist_ok=True)
            (ctx.wd / "build/synth/synth.ys").write_text("\n".join(script) + "\n")
            run = ctx.run(["yosys", "-s", "build/synth/synth.ys"], timeout_s)
            stat = ctx.wd / "build/synth/stat.json"
            parsed = yosys_synth.parse(run.log, stat.read_text() if stat.exists() else None)
            ctx.payload.update(parsed)
            if run.rc != 0 or not (ctx.wd / out).exists():
                err = next((l for l in run.log.splitlines() if l.startswith("ERROR")), f"yosys rc={run.rc}")
                raise ToolFail("tool_failed", err)
            ctx.capture = ["build/synth"]
            ctx.meta = {"metrics": {"synth": {k: parsed[k] for k in ("cells", "area_um2", "logic_depth")}},
                        "synth": {"top": top, "liberty": liberty_ref, "netlist": out}}
            ctx.after_capture = lambda new_ref: ctx.payload.update(netlist_ref=f"{new_ref}:{out}")

        return self._execute("synth_run", args, design_ref, {"top": top}, body, budget)

    def t_sim_run(self, design_ref: str, tb_ref: str | list[str], simulator: str = "icarus", seed: int = 1,
                  timeout_s: float = 300, top: str = "tb", files: list[str] | None = None, coverage: bool = False,
                  window_ns: float = 50, budget=DEFAULT_BUDGET):
        tbs = [tb_ref] if isinstance(tb_ref, str) else list(tb_ref)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", top):
            raise StoreError(f"invalid top module name {top!r}")
        args = {"tb_ref": tbs, "simulator": simulator, "seed": seed, "timeout_s": timeout_s, "top": top,
                "files": files, "coverage": coverage, "window_ns": window_ns}

        def body(ctx: Ctx):
            ctx.seed = seed
            ref = self.store.resolve(design_ref)
            mf = self.store.manifest(ref)["files"]
            for t in tbs:
                self._need(mf, t, "tb_ref")
            fl = [f for f in self._files(ref, files) if f not in tbs] + tbs
            (ctx.wd / "build/sim").mkdir(parents=True, exist_ok=True)
            if all(t.endswith(".py") for t in tbs):
                return self._cocotb(ctx, [f for f in fl if f not in tbs], tbs, simulator, top, seed, timeout_s)
            if simulator == "icarus":
                comp = ctx.run(["iverilog", "-g2012", "-o", "build/sim/sim.vvp", "-s", top] + fl, timeout_s)
                if comp.rc != 0:
                    raise ToolFail("tool_failed", _first_error(comp.log))
                run = ctx.run(["vvp", "-n", "build/sim/sim.vvp", f"+seed={seed}"], timeout_s)
            elif simulator == "verilator":
                mode = ["--binary"]
                if coverage:
                    shutil.copy(COV_MAIN, ctx.wd / "build/sim/cov_main.cpp")
                    mode = ["--cc", "--exe", "--build", "--coverage", "-CFLAGS",
                            f'-DTOP_HEADER=\\"V{top}.h\\" -DTOP_CLASS=V{top}']
                comp = ctx.run(["verilator", *mode, "--timing", "--trace", "-Wno-fatal", "-Wno-lint", "-Wno-style",
                                "--top-module", top, "-Mdir", "build/sim/obj", "-j", "1"] + fl
                               + ([self._abs(ctx, "build/sim/cov_main.cpp")] if coverage else []), timeout_s)
                if comp.rc != 0:
                    raise ToolFail("tool_failed", _first_error(comp.log))
                run = ctx.run([f"build/sim/obj/V{top}", f"+seed={seed}", f"+verilator+seed+{seed}"], timeout_s)
            else:
                raise ToolFail("bad_input", f"simulator must be icarus|verilator, got {simulator!r}")
            parsed = sim_log.parse(run.log)
            ff = parsed["first_failure"]
            if ff and ff["time_ns"] is not None:
                vcds = sorted(ctx.wd.glob("*.vcd"))
                if vcds:
                    ff["signal_window"] = vcd.signal_window(vcds[0].read_text(errors="replace"), ff["time_ns"],
                                                            before_ns=window_ns, after_ns=window_ns / 5)
            ctx.payload.update(parsed)
            if coverage and simulator == "verilator":
                dat = ctx.wd / "coverage.dat"
                if dat.exists():
                    ctx.payload["coverage"] = verilator_coverage.parse(dat.read_text(errors="replace"))
                    ctx.payload["coverage_ref"] = ctx.blob(str(dat.relative_to(ctx.wd)))

        payload = {"simulator": simulator, "seed": seed, "status": "error"}
        resp = self._execute("sim_run", args, design_ref, payload, body, budget)
        if resp["error"] and resp["error"]["kind"] == "timeout":
            resp["status"] = "timeout"
        return resp

    def _cocotb(self, ctx: Ctx, hdl: list[str], tests: list[str], simulator: str, top: str, seed: int, timeout_s):
        """tb_ref = cocotb test module(s) (.py); `top` is then the DUT module. Results come from JUnit XML."""
        mods = [Path(t).stem for t in tests]
        script = (
            "import os, sys\nfrom cocotb_tools.runner import get_runner\n"
            "os.environ.pop('PYTEST_CURRENT_TEST', None)  # cocotb changes behaviour when it thinks pytest drives it\n"
            f"sys.path[:0] = {sorted({str(Path(t).parent) for t in tests})!r}\n"
            f"r = get_runner({simulator!r})\n"
            f"r.build(sources={hdl!r}, hdl_toplevel={top!r}, build_dir='build/sim', always=True,\n"
            "        timescale=('1ns', '1ps'))\n"
            f"r.test(test_module={mods!r}, hdl_toplevel={top!r}, build_dir='build/sim', test_dir='.', seed={seed},\n"
            f"       results_xml='build/sim/results.xml', extra_env={{'PYTHONPATH': ':'.join(sys.path[:{len(tests)}])}})\n")
        (ctx.wd / "build/sim/run_cocotb.py").write_text(script)
        run = ctx.run(["python3", "build/sim/run_cocotb.py"], timeout_s)
        xml = ctx.wd / "build/sim/results.xml"
        if not xml.exists():
            raise ToolFail("tool_failed", _first_error(run.log))
        ctx.payload.update(junit_xml.parse(xml.read_text()))

    # ------------------------------------------------------------------ formal / equiv
    def t_formal_run(self, design_ref: str, sby_ref: str, mode: str | None = None, depth: int | None = None,
                     timeout_s: float = 900, budget=DEFAULT_BUDGET):
        args = {"sby_ref": sby_ref, "mode": mode, "depth": depth, "timeout_s": timeout_s}
        if mode not in (None, "bmc", "prove", "cover", "live"):
            raise StoreError(f"mode must be bmc|prove|cover|live, got {mode!r}")

        def body(ctx: Ctx):
            self._need(self.store.manifest(self.store.resolve(design_ref))["files"], sby_ref, "sby_ref")
            sby = ctx.wd / sby_ref
            text = _patch_sby(sby.read_text(), mode, depth)
            sby.write_text(text)
            m = re.search(r"^\s*mode\s+(\w+)", text, re.M)
            d = re.search(r"^\s*depth\s+(\d+)", text, re.M)
            ctx.payload.update(mode=m.group(1) if m else "bmc", depth=int(d.group(1)) if d else None)
            run = ctx.run(["sby", "-f", sby.name], timeout_s, cwd=sby.parent)
            parsed = sby_status.parse(run.log)
            for p in parsed["failed_props"]:
                tp = p.pop("trace_path")
                p["trace_ref"] = ctx.blob(str((sby.parent / tp).relative_to(ctx.wd))) if tp else None
            ctx.payload.update(parsed)

        return self._execute("formal_run", args, design_ref, {"mode": mode or "bmc", "depth": depth,
                                                              "status": "error"}, body, budget)

    def t_equiv_run(self, gold_ref: str, gate_ref: str, top: str, gold_files: list[str] | None = None,
                    gate_files: list[str] | None = None, liberty_ref: str | None = None, seq: int = 5,
                    timeout_s: float = 900, budget=DEFAULT_BUDGET):
        gold, gate = self.store.resolve(gold_ref), self.store.resolve(gate_ref)
        gate_m = self.store.manifest(gate)
        args = {"gold_ref": gold, "gate_ref": gate, "top": top, "gold_files": gold_files, "gate_files": gate_files,
                "liberty_ref": liberty_ref, "seq": seq, "timeout_s": timeout_s}
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", top):
            raise StoreError(f"invalid top module name {top!r}")
        synth_meta = gate_m["meta"].get("synth", {})
        lib = liberty_ref or synth_meta.get("liberty")
        gfiles = gate_files or ([synth_meta["netlist"]] if synth_meta.get("netlist") in gate_m["files"] else None)

        def body(ctx: Ctx):
            gold_fl = self._files(gold, gold_files)
            gate_fl = gfiles or self._files(gate, None)
            self.store.materialize(gold, ctx.wd / "gold")
            self.store.materialize(gate, ctx.wd / "gate")
            if lib:
                self._need(gate_m["files"], lib, "liberty_ref")
            script = [f"read_verilog -sv {' '.join('gold/' + f for f in gold_fl)}", f"prep -top {top}",
                      "design -stash gold"]
            if lib:
                script.append(f"read_liberty gate/{lib}")  # functional cell models, flattened below
            script += [f"read_verilog -sv {' '.join('gate/' + f for f in gate_fl)}", f"hierarchy -top {top}",
                       "flatten", f"prep -top {top}", "design -stash gate",
                       f"design -copy-from gold -as gold {top}", f"design -copy-from gate -as gate {top}",
                       "equiv_make gold gate equiv", "hierarchy -top equiv", "async2sync",
                       f"equiv_simple -seq {seq}", f"equiv_induct -seq {seq}", "equiv_status"]
            (ctx.wd / "equiv.ys").write_text("\n".join(script) + "\n")
            run = ctx.run(["yosys", "-s", "equiv.ys"], timeout_s)
            parsed = yosys_equiv.parse(run.log)
            ctx.payload.update(parsed)
            if parsed["equivalent"] is None:
                raise ToolFail("tool_failed", _first_error(run.log))

        return self._execute("equiv_run", args, gate, {}, body, budget, ref_arg=None)

    # ------------------------------------------------------------------ ORFS flow / STA
    def _orfs_cmd(self, config: str, threads: int, target: list[str], extra: dict | None = None) -> list[str]:
        make = ["make", "-C", "/OpenROAD-flow-scripts/flow", f"DESIGN_CONFIG={CONTAINER_WORKDIR}/{config}",
                f"WORK_HOME={CONTAINER_WORKDIR}/orfs", f"DESIGN_DIR={CONTAINER_WORKDIR}", f"NUM_CORES={threads}"]
        make += [f"{k}={v}" for k, v in (extra or {}).items()] + target
        return ["bash", "-c", "source /OpenROAD-flow-scripts/env.sh >/dev/null && " + shlex.join(make)]

    def t_flow_run_stage(self, design_ref: str, stage: str, overrides: dict | None = None, config: str = "config.mk",
                         threads: int = 1, timeout_s: float = 7200, budget=DEFAULT_BUDGET):
        overrides = overrides or {}
        args = {"stage": stage, "overrides": overrides, "config": config, "threads": threads, "timeout_s": timeout_s}
        if stage not in ORFS_STAGES:
            raise StoreError(f"stage must be one of {ORFS_STAGES}")
        for k, v in overrides.items():  # trust boundary: these become make variables
            if not _OVERRIDE_KEY.match(k) or not _OVERRIDE_VAL.match(str(v)):
                raise StoreError(f"invalid override {k}={v!r}")

        def body(ctx: Ctx):
            ctx.threads = threads
            self._need(self.store.manifest(self.store.resolve(design_ref))["files"], config, "config")
            later = ORFS_STAGES[ORFS_STAGES.index(stage):]
            prefixes = tuple(p for s in later for p in ORFS_PREFIX[s])
            for sub in ("results", "logs", "reports"):
                for f in (ctx.wd / "orfs" / sub).glob(f"*/*/*/*"):
                    if f.name.startswith(prefixes):
                        f.unlink()
            run = ctx.run(self._orfs_cmd(config, threads, [stage], {k: str(v) for k, v in overrides.items()}),
                          timeout_s, runner=self.orfs)
            mine = ORFS_PREFIX[stage]
            all_steps = {f.stem: json.loads(f.read_text()) for f in (ctx.wd / "orfs/logs").glob("*/*/*/*.json")}
            parsed = orfs_metrics.parse({k: v for k, v in all_steps.items() if k.startswith(mine)})
            # platform units are only emitted by some steps (floorplan); take them from any step
            parsed["units"] = orfs_metrics.parse(all_steps)["units"]
            ctx.payload.update(parsed)
            if run.rc != 0:
                errs = _ORFS_ERROR.findall(run.log)
                raise ToolFail("tool_failed", errs[0] if errs else f"make {stage} rc={run.rc}")
            ctx.capture = ["orfs"]
            ctx.meta = {"metrics": {stage: parsed["metrics"]}}

        return self._execute("flow_run_stage", args, design_ref, {"stage": stage}, body, budget)

    def _orfs_design(self, ctx: Ctx, stage: str | None) -> tuple[str, str]:
        """Pick the ODB/SDC of `stage` (default: latest in the snapshot) -> (stage, Tcl that loads it)."""
        results = sorted(ctx.wd.glob("orfs/results/*/*/*"))
        if not results:
            raise ToolFail("bad_input", "no ORFS results in snapshot; run flow_run_stage first")
        have = {p.name for p in results[0].iterdir()}
        pick = [(s, b) for s, b in ORFS_ODB if f"{b}.odb" in have and (stage is None or s == stage)]
        if not pick:
            raise ToolFail("bad_input", f"no ODB for stage {stage!r} in snapshot")
        st, base = pick[0]
        sdc = f"{base}.sdc" if f"{base}.sdc" in have else next(
            (f"{b}.sdc" for _, b in ORFS_ODB if f"{b}.sdc" in have), "")
        return st, f"source $::env(SCRIPTS_DIR)/load.tcl\nload_design {base}.odb {sdc}\n"

    def _orfs_tcl(self, ctx: Ctx, tcl: str, config: str, threads: int, timeout_s: float):
        (ctx.wd / ".edaport_run.tcl").write_text(tcl)
        return ctx.run(self._orfs_cmd(config, threads, ["run"], {"RUN_SCRIPT": f"{CONTAINER_WORKDIR}/.edaport_run.tcl"}),
                       timeout_s, runner=self.orfs)

    def t_sta_report_paths(self, design_ref: str, corner: str | None = None, k: int = 10, group: str | None = None,
                           stage: str | None = None, path_delay: str = "max", config: str = "config.mk",
                           threads: int = 1, timeout_s: float = 1800, budget=DEFAULT_BUDGET):
        args = {"corner": corner, "k": k, "group": group, "stage": stage, "path_delay": path_delay, "config": config,
                "threads": threads, "timeout_s": timeout_s}
        if path_delay not in ("max", "min"):
            raise StoreError("path_delay must be max (setup) or min (hold)")
        for name, v in (("corner", corner), ("group", group)):
            if v is not None and not re.match(r"^[A-Za-z0-9_.\-]+$", v):
                raise StoreError(f"invalid {name} {v!r}")
        k = max(1, min(int(k), 100))

        def body(ctx: Ctx):
            ctx.threads = threads
            st, load = self._orfs_design(ctx, stage)
            ctx.payload["stage"] = st
            opts = f"-path_delay {path_delay} -group_path_count {k} -format json"
            opts += f" -path_group {group}" if group else ""
            opts += f" -corner {corner}" if corner else ""
            tcl = (load + f'puts "EDAPORT_WNS [sta::worst_slack_cmd {path_delay}]"\n'
                   + f'puts "EDAPORT_TNS [sta::total_negative_slack_cmd {path_delay}]"\n'
                   + 'foreach net [get_nets *] { puts "EDAPORT_FO [get_full_name $net] '
                     '[llength [get_pins -of_objects $net -filter "direction == input"]]" }\n'
                   + f"report_checks {opts}\n")
            run = self._orfs_tcl(ctx, tcl, config, threads, timeout_s)
            if run.rc != 0 or "EDAPORT_WNS" not in run.log:
                errs = _ORFS_ERROR.findall(run.log)
                raise ToolFail("tool_failed", errs[0] if errs else f"sta rc={run.rc}")
            ctx.payload.update(opensta_json.parse(run.log))

        return self._execute("sta_report_paths", args, design_ref, {"stage": stage or "latest", "corner": corner,
                                                                         "path_delay": path_delay}, body, budget)

    def t_tcl_run(self, design_ref: str, script: str, stage: str | None = None, config: str = "config.mk",
                  threads: int = 1, timeout_s: float = 1800, budget=DEFAULT_BUDGET):
        """Escape hatch: run OpenROAD Tcl on a loaded stage. Off unless EDAPORT_ALLOW_TCL=1; the script is logged."""
        args = {"script": script, "stage": stage, "config": config, "threads": threads, "timeout_s": timeout_s}
        if os.environ.get("EDAPORT_ALLOW_TCL") != "1":
            raise StoreError("Tcl passthrough is disabled; the operator must set EDAPORT_ALLOW_TCL=1")
        if len(script) > 20_000:
            raise StoreError("script too long (max 20000 chars)")

        def body(ctx: Ctx):
            ctx.threads = threads
            st, load = self._orfs_design(ctx, stage)
            ctx.payload["stage"] = st
            run = self._orfs_tcl(ctx, load + 'puts "@@EDAPORT_TCL_BEGIN"\n' + script + "\n", config, threads, timeout_s)
            out = run.log.partition("@@EDAPORT_TCL_BEGIN\n")[2].splitlines()
            out = [l for l in out if not l.startswith(("make: ", "Elapsed time:"))]
            ctx.payload.update(lines=out[-200:], total_lines=len(out))
            if run.rc != 0:
                errs = _ORFS_ERROR.findall(run.log)
                raise ToolFail("tool_failed", errs[0] if errs else f"openroad rc={run.rc}")

        return self._execute("tcl_run", args, design_ref, {"stage": stage or "latest"}, body, budget)

    # ------------------------------------------------------------------ physical verification
    def _orfs_signoff(self, ctx: Ctx, target: str, config: str, threads: int, timeout_s: float):
        ctx.threads = threads
        if not list(ctx.wd.glob("orfs/results/*/*/*/6_final.gds")):
            raise ToolFail("bad_input", f"ORFS {target} needs the finish stage (6_final.gds); run flow_run_stage finish")
        run = ctx.run(self._orfs_cmd(config, threads, [target]), timeout_s, runner=self.orfs)
        if run.rc != 0:
            errs = _ORFS_ERROR.findall(run.log)
            raise ToolFail("tool_failed", errs[0] if errs else f"make {target} rc={run.rc}")
        return run

    def t_drc_run(self, design_ref: str, deck: str | None = None, gds: str | None = None, config: str = "config.mk",
                  threads: int = 1, timeout_s: float = 1800, budget=DEFAULT_BUDGET):
        """deck=None: the PDK's KLayout deck on the ORFS finish-stage GDS. Otherwise: a deck file in the snapshot."""
        args = {"deck": deck, "gds": gds, "config": config, "threads": threads, "timeout_s": timeout_s}

        def body(ctx: Ctx):
            if deck is None:
                self._orfs_signoff(ctx, "drc", config, threads, timeout_s)
                rpt = next(iter(ctx.wd.glob("orfs/reports/*/*/*/6_drc.lyrdb")), None)
                if rpt is None:
                    raise ToolFail("tool_failed", "ORFS produced no 6_drc.lyrdb")
                ctx.payload.update(klayout_lyrdb.parse(rpt.read_text()))
                return
            mf = self.store.manifest(self.store.resolve(design_ref))["files"]
            self._need(mf, deck, "deck")
            layout = gds or next((f for f in sorted(mf, key=lambda f: ("6_final" not in f, f))
                                  if f.endswith((".gds", ".gds.gz", ".oas"))), None)
            if not layout:
                raise ToolFail("bad_input", "no layout (.gds/.oas) in snapshot; pass gds=")
            self._need(mf, layout, "gds")
            (ctx.wd / "build/drc").mkdir(parents=True, exist_ok=True)
            run = ctx.run(["klayout", "-b", "-r", deck, "-rd", f"input={layout}", "-rd",
                           "report=build/drc/report.lyrdb"], timeout_s)
            rpt = ctx.wd / "build/drc/report.lyrdb"
            if run.rc != 0 or not rpt.exists():
                raise ToolFail("tool_failed", _first_error(run.log))
            ctx.payload.update(klayout_lyrdb.parse(rpt.read_text()))

        return self._execute("drc_run", args, design_ref, {}, body, budget)

    def t_lvs_run(self, design_ref: str, layout: str | None = None, schematic: str | None = None,
                  top: str | None = None, schematic_top: str | None = None, setup: str | None = None,
                  config: str = "config.mk", threads: int = 1, timeout_s: float = 1800, budget=DEFAULT_BUDGET):
        """No layout/schematic: the PDK's KLayout LVS on the ORFS finish stage. Otherwise: Netgen on two netlists."""
        args = {"layout": layout, "schematic": schematic, "top": top, "schematic_top": schematic_top,
                "setup": setup, "config": config, "threads": threads, "timeout_s": timeout_s}
        flow_mode = layout is None and schematic is None
        if not flow_mode and not (layout and schematic and top):
            raise StoreError("give layout, schematic and top together, or none of them for ORFS LVS")
        for v in (top, schematic_top):
            if v is not None and not re.match(r"^[A-Za-z0-9_$.\-]+$", v):
                raise StoreError(f"invalid cell name {v!r}")

        def body(ctx: Ctx):
            if flow_mode:
                log = self._orfs_signoff(ctx, "lvs", config, threads, timeout_s).log
                match = True if "Netlists match" in log else (False if "Netlists don't match" in log else None)
                bad = [] if match else [l.strip() for l in log.splitlines() if re.match(r"\s*(ERROR|WARNING)\s*:", l)]
                ctx.payload.update(match=match, mismatches=bad)
                return
            mf = self.store.manifest(self.store.resolve(design_ref))["files"]
            for p, what in ((layout, "layout"), (schematic, "schematic")) + (((setup, "setup"),) if setup else ()):
                self._need(mf, p, what)
            (ctx.wd / "build/lvs").mkdir(parents=True, exist_ok=True)
            netgen = "netgen-lvs" if shutil.which("netgen-lvs") else "netgen"
            run = ctx.run([netgen, "-batch", "lvs", f"{layout} {top}", f"{schematic} {schematic_top or top}",
                           setup or "nosetup", "build/lvs/lvs.out", "-json"], timeout_s)
            out = ctx.wd / "build/lvs/lvs.out"
            if not out.exists():
                raise ToolFail("tool_failed", _first_error(run.log))
            comp = ctx.wd / "build/lvs/lvs.json"
            ctx.payload.update(netgen_lvs.parse(out.read_text(errors="replace"),
                                                comp.read_text() if comp.exists() else None))

        return self._execute("lvs_run", args, design_ref, {}, body, budget)

    # ------------------------------------------------------------------ snapshots
    def t_snapshot_import(self, path: str, label: str | None = None, pdk_ref: str | None = None,
                          budget=DEFAULT_BUDGET):
        src = Path(path).resolve()
        if not any(src == r or r in src.parents for r in self.import_roots):
            raise StoreError(f"{path} is outside EDAPORT_IMPORT_ROOTS (set it to the directories agents may import)")
        if not src.is_dir():
            raise StoreError(f"{path} is not a directory")
        ref = self.store.import_dir(src, {"pdk_ref": pdk_ref} if pdk_ref else {})
        prev = self.store.branch(ref, label) if label else None
        n = len(self.store.manifest(ref)["files"])
        return self._record("snapshot_import", {"path": str(src), "label": label, "pdk_ref": pdk_ref}, None, ref,
                            {"files": n, "branch": label, "previous_ref": prev}, budget)

    def t_snapshot_write(self, design_ref: str, path: str, content: str | None, branch: str | None = None,
                         budget=DEFAULT_BUDGET):
        ref = self.store.resolve(design_ref)
        path = check_relpath(path)
        if path.startswith(TOOL_OWNED):
            raise StoreError(f"{path}: {TOOL_OWNED} are tool-owned outputs and not writable")
        data = None if content is None else content.encode()
        if data is not None and len(data) > MAX_WRITE_BYTES:
            raise StoreError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        new = self.store.write_file(ref, path, data)
        prev = self.store.branch(new, branch) if branch else None
        args = {"design_ref": ref, "path": path, "content_digest": sha256(data or b""), "delete": data is None,
                "branch": branch}
        return self._record("snapshot_write", args, ref, new,
                            {"files": len(self.store.manifest(new)["files"]), "branch": branch, "previous_ref": prev},
                            budget)

    def t_snapshot_read(self, design_ref: str, path: str | None = None, offset: int = 0, max_lines: int = 200,
                        budget=DEFAULT_BUDGET):
        ref = self.store.resolve(design_ref)
        args = {"design_ref": ref, "path": path, "offset": offset, "max_lines": max_lines}
        if path is None:
            return self._record("snapshot_read", args, ref, ref, {"files": sorted(self.store.manifest(ref)["files"])},
                                budget)
        data = self.store.read_file(ref, path)
        if b"\x00" in data[:4096]:
            raise StoreError(f"{path} is binary")
        page = _page(data.decode(errors="replace").splitlines(), None, offset, max_lines)
        return self._record("snapshot_read", args, ref, ref, {"path": path, **page}, budget)

    def t_snapshot_branch(self, design_ref: str, label: str, budget=DEFAULT_BUDGET):
        ref = self.store.resolve(design_ref)
        prev = self.store.branch(ref, label)
        return self._record("snapshot_branch", {"design_ref": ref, "label": label}, ref, ref,
                            {"branch": label, "previous_ref": prev}, budget)

    def t_snapshot_restore(self, design_ref: str, label: str = "main", budget=DEFAULT_BUDGET):
        """Roll a branch back (or forward) to design_ref. previous_ref lets the agent undo the restore."""
        ref = self.store.resolve(design_ref)
        prev = self.store.branch(ref, label)
        return self._record("snapshot_restore", {"design_ref": ref, "label": label}, prev, ref,
                            {"branch": label, "previous_ref": prev}, budget)

    def t_snapshot_diff(self, a_ref: str, b_ref: str, budget=DEFAULT_BUDGET):
        a, b = self.store.resolve(a_ref), self.store.resolve(b_ref)
        return self._record("snapshot_diff", {"a_ref": a, "b_ref": b}, a, b,
                            {"a_ref": a, "b_ref": b, **self.store.diff(a, b)}, budget)

    # ------------------------------------------------------------------ evidence + replay
    def t_log_page(self, run_id: str, pattern: str | None = None, offset: int = 0, max_lines: int = 200,
                   budget=DEFAULT_BUDGET):
        rec = self._run(run_id)
        if not rec.get("log_ref"):
            raise StoreError(f"{run_id} has no raw log")
        lines = self.store.get_blob(rec["log_ref"]).decode(errors="replace").splitlines()
        try:
            page = _page(lines, pattern, offset, max_lines)
        except re.error as e:
            raise StoreError(f"bad pattern: {e}")
        return self._record("log_page", {"run_id": run_id, "pattern": pattern, "offset": offset,
                                         "max_lines": max_lines}, None, None,
                            {"page_of": run_id, "pattern": pattern, **page}, budget)

    def t_result_page(self, more_handle: str, path: str = "", offset: int = 0, limit: int = 50,
                      budget=DEFAULT_BUDGET):
        rec = self._run(more_handle)
        full = json.loads(self.store.get_blob(rec["result_ref"]))
        full["run_id"] = rec["run_id"]
        try:
            val = get_path(full, path)
        except (KeyError, IndexError, ValueError) as e:
            raise StoreError(str(e))
        limit = max(1, min(int(limit), 500))
        payload = {"page_of": more_handle, "path": path, "offset": offset}
        if isinstance(val, list):
            end = offset + limit
            payload.update(items=val[offset:end], total=len(val), next_offset=end if end < len(val) else None)
        else:
            payload["value"] = val
        return self._record("result_page", {"more_handle": more_handle, "path": path, "offset": offset,
                                            "limit": limit}, None, None, payload, budget)

    def t_coverage_report(self, coverage_ref: str, budget=DEFAULT_BUDGET):
        """Per-file detail of a coverage_ref returned by sim_run(simulator="verilator", coverage=True)."""
        text = self.store.get_blob(coverage_ref).decode(errors="replace")
        files = verilator_coverage.report(text)
        if not files:
            raise StoreError(f"{coverage_ref} is not a verilator coverage.dat")
        return self._record("coverage_report", {"coverage_ref": coverage_ref}, None, None,
                            {**verilator_coverage.parse(text), "files": files}, budget)

    def t_replay_run(self, run_id: str, budget=DEFAULT_BUDGET):
        """Re-execute a recorded call with identical inputs and compare result digests."""
        rec = self._run(run_id)
        if not rec.get("log_ref"):
            raise StoreError(f"{rec['tool']} does not execute an EDA tool; nothing to replay")
        again = self.call(rec["tool"], **rec["args"], budget_tokens=10 ** 9)
        new = self._run(again["run_id"])
        payload = {"original_run_id": run_id, "match": new["result_digest"] == rec["result_digest"],
                   "original_digest": rec["result_digest"], "replay_digest": new["result_digest"],
                   "output_ref_match": new.get("output_ref") == rec.get("output_ref")}
        return self._record("replay_run", {"run_id": run_id}, rec.get("parent_ref"), new.get("output_ref"),
                            payload, budget)

    def _run(self, run_id: str) -> dict:
        try:
            return self.log.get(run_id)
        except KeyError as e:
            raise StoreError(str(e))

    def gc(self, keep_runs: int | None = None) -> int:
        """Evict objects unreachable from branches and the replay log (optionally only its last keep_runs runs)."""
        recs = self.log.records()
        keep = set()
        for rec in recs[-keep_runs:] if keep_runs else recs:
            keep.update(v for k, v in rec.items() if k.endswith("_ref") and isinstance(v, str))
            keep.update(v for k, v in rec.get("args", {}).items() if k.endswith("_ref") and isinstance(v, str))
            if self.store.has(rec["result_ref"]):  # blobs named inside results (trace_ref, coverage_ref)
                keep.update(re.findall(r"sha256:[0-9a-f]{64}", self.store.get_blob(rec["result_ref"]).decode()))
        return self.store.gc(keep)


def _page(lines: list[str], pattern: str | None, offset: int, max_lines: int) -> dict:
    if pattern:
        rx = re.compile(pattern)
        lines = [f"{i + 1}: {l}" for i, l in enumerate(lines) if rx.search(l)]
    max_lines = max(1, min(int(max_lines), 1000))
    offset = max(0, int(offset))
    end = offset + max_lines
    return {"lines": lines[offset:end], "offset": offset, "total_lines": len(lines),
            "next_offset": end if end < len(lines) else None}


def _first_error(log: str) -> str:
    for line in log.splitlines():
        if re.search(r"error|ERROR|%Error|Error:", line):
            return line.strip()[:500]
    tail = [l for l in log.splitlines() if l.strip()]
    return tail[-1][:500] if tail else "tool failed with no output"


def _patch_sby(text: str, mode: str | None, depth: int | None) -> str:
    """Override `mode`/`depth` in the [options] section of a .sby file (creating the section if needed)."""
    want = {k: v for k, v in (("mode", mode), ("depth", depth)) if v is not None}
    if not want:
        return text
    out, in_opts, seen = [], False, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_opts = s == "[options]"
            out.append(line)
            if in_opts:
                seen = True
                out += [f"{k} {v}" for k, v in want.items()]
            continue
        if in_opts and s.split(" ")[0] in want:
            continue  # replaced by the lines inserted after the header
        out.append(line)
    if not seen:
        out = ["[options]"] + [f"{k} {v}" for k, v in want.items()] + [""] + out
    return "\n".join(out) + "\n"

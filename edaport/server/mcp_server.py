"""MCP surface. Tool names use underscores (`lint_run` for spec `lint.run`): several MCP clients,
including the Anthropic API, only accept ^[a-zA-Z0-9_-]{1,64}$ tool names.

Every tool returns structured content validated against its pydantic schema (see edaport.models).
"""

from functools import partial
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .. import models as M
from .engine import DEFAULT_BUDGET, Engine

INSTRUCTIONS = """EDAPort wraps open-source EDA tools (Verilator, Icarus, Yosys, SymbiYosys, OpenROAD/ORFS, OpenSTA,
KLayout, Netgen). Workflow: snapshot_import a design directory -> get a design_ref (or use a branch label) ->
run tools on it. Tools that change the design (synth_run, flow_run_stage, snapshot_write) return a NEW design_ref;
old refs stay valid, so branching and rollback are free. Responses are bounded to ~budget_tokens: when
truncated=true, `omitted` says what was cut and result_page(more_handle, path) pages it in full. Raw tool output
is never lost: log_page(run_id, pattern) greps it. replay_run(run_id) re-executes a call and checks determinism."""

Ref = Annotated[str, Field(description="design_ref (sha256:...) or branch label")]
Budget = Annotated[int, Field(description="Max response size in tokens (approx.)")]


def build_server(engine: Engine | None = None) -> MCPServer:
    engine = engine or Engine()
    mcp = MCPServer(name="edaport", instructions=INSTRUCTIONS)

    async def call(name: str, model, **kwargs):
        res = await anyio.to_thread.run_sync(partial(engine.call, name, **kwargs))
        return model.model_validate(res)

    @mcp.tool()
    async def lint_run(design_ref: Ref, tool: str = "verilator", files: list[str] | None = None,
                       top: str | None = None, timeout_s: float = 300,
                       budget_tokens: Budget = DEFAULT_BUDGET) -> M.LintResult:
        """Lint RTL (verilator|iverilog). Returns located errors, warnings and per-code warning counts.
        files defaults to rtl/ or src/ HDL files in the snapshot."""
        return await call("lint_run", M.LintResult, design_ref=design_ref, tool=tool, files=files, top=top,
                          timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def synth_run(design_ref: Ref, top: str, liberty_ref: str | None = None, files: list[str] | None = None,
                        timeout_s: float = 900, budget_tokens: Budget = DEFAULT_BUDGET) -> M.SynthResult:
        """Yosys synthesis. Returns cells, area (with a liberty file path in the snapshot), logic depth,
        grouped warnings, and a new design_ref containing build/synth/<top>.v."""
        return await call("synth_run", M.SynthResult, design_ref=design_ref, top=top, liberty_ref=liberty_ref,
                          files=files, timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def sim_run(design_ref: Ref, tb_ref: str | list[str], simulator: str = "icarus", seed: int = 1,
                      timeout_s: float = 300, top: str = "tb", files: list[str] | None = None,
                      coverage: bool = False, window_ns: float = 50,
                      budget_tokens: Budget = DEFAULT_BUDGET) -> M.SimResult:
        """Simulate (icarus|verilator). The testbench prints `TEST <name> PASS|FAIL @<ns> msg` lines.
        On failure, first_failure.signal_window has value changes around the failure time from the VCD.
        cocotb: pass .py test module(s) as tb_ref and the DUT module as top."""
        return await call("sim_run", M.SimResult, design_ref=design_ref, tb_ref=tb_ref, simulator=simulator,
                          seed=seed, timeout_s=timeout_s, top=top, files=files, coverage=coverage,
                          window_ns=window_ns, budget_tokens=budget_tokens)

    @mcp.tool()
    async def formal_run(design_ref: Ref, sby_ref: str, mode: str | None = None, depth: int | None = None,
                         timeout_s: float = 900, budget_tokens: Budget = DEFAULT_BUDGET) -> M.FormalResult:
        """SymbiYosys run of a .sby file in the snapshot; mode (bmc|prove|cover|live) and depth override it.
        Failed properties carry the step and a trace_ref (VCD blob)."""
        return await call("formal_run", M.FormalResult, design_ref=design_ref, sby_ref=sby_ref, mode=mode,
                          depth=depth, timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def equiv_run(gold_ref: Ref, gate_ref: Ref, top: str, gold_files: list[str] | None = None,
                        gate_files: list[str] | None = None, liberty_ref: str | None = None, seq: int = 5,
                        timeout_s: float = 900, budget_tokens: Budget = DEFAULT_BUDGET) -> M.EquivResult:
        """Yosys equivalence (equiv_make/simple/induct) of gold RTL vs gate netlist. Defaults gate_files and
        liberty to what synth_run recorded in gate_ref."""
        return await call("equiv_run", M.EquivResult, gold_ref=gold_ref, gate_ref=gate_ref, top=top,
                          gold_files=gold_files, gate_files=gate_files, liberty_ref=liberty_ref, seq=seq,
                          timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def flow_run_stage(design_ref: Ref, stage: str, overrides: dict[str, Any] | None = None,
                             config: str = "config.mk", threads: int = 1, timeout_s: float = 7200,
                             budget_tokens: Budget = DEFAULT_BUDGET) -> M.FlowResult:
        """Run one ORFS stage (synth|floorplan|place|cts|grt|route|finish) in the pinned ORFS image.
        overrides are ORFS make variables (e.g. {"PLACE_DENSITY": 0.62}). threads=1 for exact replay."""
        return await call("flow_run_stage", M.FlowResult, design_ref=design_ref, stage=stage, overrides=overrides,
                          config=config, threads=threads, timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def sta_report_paths(design_ref: Ref, corner: str | None = None, k: int = 10, group: str | None = None,
                               stage: str | None = None, path_delay: str = "max", config: str = "config.mk",
                               threads: int = 1, timeout_s: float = 1800,
                               budget_tokens: Budget = DEFAULT_BUDGET) -> M.StaResult:
        """OpenSTA on the latest (or given) ORFS stage: WNS/TNS in ps and the k worst paths per group
        (path_delay max = setup, min = hold), one row per cell with its delay and net fanout."""
        return await call("sta_report_paths", M.StaResult, design_ref=design_ref, corner=corner, k=k, group=group,
                          stage=stage, path_delay=path_delay, config=config, threads=threads, timeout_s=timeout_s,
                          budget_tokens=budget_tokens)

    @mcp.tool()
    async def tcl_run(design_ref: Ref, script: str, stage: str | None = None, config: str = "config.mk",
                      threads: int = 1, timeout_s: float = 1800,
                      budget_tokens: Budget = DEFAULT_BUDGET) -> M.TclResult:
        """Escape hatch: run OpenROAD Tcl on a loaded ORFS stage. Disabled unless the operator sets
        EDAPORT_ALLOW_TCL=1. The script is recorded in the replay log. Read-only: outputs are not captured."""
        return await call("tcl_run", M.TclResult, design_ref=design_ref, script=script, stage=stage, config=config,
                          threads=threads, timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def coverage_report(coverage_ref: str, budget_tokens: Budget = DEFAULT_BUDGET) -> M.CoverageReport:
        """Per-file coverage and uncovered source lines for a coverage_ref from sim_run(verilator, coverage=true)."""
        return await call("coverage_report", M.CoverageReport, coverage_ref=coverage_ref,
                          budget_tokens=budget_tokens)

    @mcp.tool()
    async def drc_run(design_ref: Ref, deck: str | None = None, gds: str | None = None, config: str = "config.mk",
                      threads: int = 1, timeout_s: float = 1800,
                      budget_tokens: Budget = DEFAULT_BUDGET) -> M.DrcResult:
        """KLayout DRC: violations grouped per rule with count and the first marker's bbox (um).
        No deck: the PDK deck on the ORFS finish-stage GDS. With deck: a deck file in the snapshot."""
        return await call("drc_run", M.DrcResult, design_ref=design_ref, deck=deck, gds=gds, config=config,
                          threads=threads, timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def lvs_run(design_ref: Ref, layout: str | None = None, schematic: str | None = None,
                      top: str | None = None, schematic_top: str | None = None, setup: str | None = None,
                      config: str = "config.mk", threads: int = 1, timeout_s: float = 1800,
                      budget_tokens: Budget = DEFAULT_BUDGET) -> M.LvsResult:
        """LVS. Only design_ref: the PDK's KLayout LVS on the ORFS finish stage. With layout, schematic and top:
        Netgen between two netlists in the snapshot."""
        return await call("lvs_run", M.LvsResult, design_ref=design_ref, layout=layout, schematic=schematic, top=top,
                          schematic_top=schematic_top, setup=setup, config=config, threads=threads,
                          timeout_s=timeout_s, budget_tokens=budget_tokens)

    @mcp.tool()
    async def snapshot_import(path: str, label: str | None = None, pdk_ref: str | None = None) -> M.SnapshotResult:
        """Import a directory (under EDAPORT_IMPORT_ROOTS) as a content-addressed snapshot; optionally label it."""
        return await call("snapshot_import", M.SnapshotResult, path=path, label=label, pdk_ref=pdk_ref)

    @mcp.tool()
    async def snapshot_read(design_ref: Ref, path: str | None = None, offset: int = 0, max_lines: int = 200,
                            budget_tokens: Budget = DEFAULT_BUDGET) -> M.FileView:
        """List snapshot files (no path) or read a text file by line window."""
        return await call("snapshot_read", M.FileView, design_ref=design_ref, path=path, offset=offset,
                          max_lines=max_lines, budget_tokens=budget_tokens)

    @mcp.tool()
    async def snapshot_write(design_ref: Ref, path: str, content: str | None,
                             branch: str | None = None) -> M.SnapshotResult:
        """Write (or delete with content=null) one file -> new design_ref. build/ and orfs/ are tool-owned.
        branch moves that label to the new ref."""
        return await call("snapshot_write", M.SnapshotResult, design_ref=design_ref, path=path, content=content,
                          branch=branch)

    @mcp.tool()
    async def snapshot_branch(design_ref: Ref, label: str) -> M.SnapshotResult:
        """Create or move a branch label to design_ref."""
        return await call("snapshot_branch", M.SnapshotResult, design_ref=design_ref, label=label)

    @mcp.tool()
    async def snapshot_restore(design_ref: Ref, label: str = "main") -> M.SnapshotResult:
        """Roll branch `label` back to design_ref; previous_ref lets you undo."""
        return await call("snapshot_restore", M.SnapshotResult, design_ref=design_ref, label=label)

    @mcp.tool()
    async def snapshot_diff(a_ref: Ref, b_ref: Ref, budget_tokens: Budget = DEFAULT_BUDGET) -> M.DiffResult:
        """Files added/removed/modified and metric deltas between two snapshots."""
        return await call("snapshot_diff", M.DiffResult, a_ref=a_ref, b_ref=b_ref, budget_tokens=budget_tokens)

    @mcp.tool()
    async def log_page(run_id: str, pattern: str | None = None, offset: int = 0, max_lines: int = 200,
                       budget_tokens: Budget = DEFAULT_BUDGET) -> M.LogPage:
        """Escape hatch to raw evidence: page or regex-grep the raw tool output of a run."""
        return await call("log_page", M.LogPage, run_id=run_id, pattern=pattern, offset=offset, max_lines=max_lines,
                          budget_tokens=budget_tokens)

    @mcp.tool()
    async def result_page(more_handle: str, path: str = "", offset: int = 0, limit: int = 50,
                          budget_tokens: Budget = DEFAULT_BUDGET) -> M.ResultPage:
        """Read a trimmed field of a truncated result in full; path is a dotted key from `omitted`."""
        return await call("result_page", M.ResultPage, more_handle=more_handle, path=path, offset=offset,
                          limit=limit, budget_tokens=budget_tokens)

    @mcp.tool()
    async def replay_run(run_id: str) -> M.ReplayResult:
        """Re-execute a recorded tool call with identical inputs; match=true if the typed result is identical."""
        return await call("replay_run", M.ReplayResult, run_id=run_id)

    return mcp


def main():
    build_server().run("stdio")

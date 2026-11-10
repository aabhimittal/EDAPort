"""Typed result schemas. Every tool response is validated against one of these before it leaves the server."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(_M):
    kind: Literal["tool_missing", "timeout", "tool_failed", "bad_input", "parse_error"]
    msg: str


class Envelope(_M):
    run_id: str | None = Field(description="Replay-log id; pass to log_page / replay_run")
    tool: str
    ok: bool = Field(description="False when the tool could not run or produced no parseable result (see error)")
    error: ToolError | None = None
    parent_ref: str | None = Field(None, description="design_ref the tool ran on")
    design_ref: str | None = Field(None, description="resulting design_ref (new if the tool mutated state)")
    truncated: bool = False
    more_handle: str | None = Field(None, description="Pass to result_page to read trimmed fields in full")
    omitted: dict[str, int] = Field(default_factory=dict, description="dotted field path -> items/chars trimmed")


class Diag(_M):
    file: str | None
    line: int | None
    code: str | None
    msg: str


class LintResult(Envelope):
    tool_name: str = ""
    errors: list[Diag] = []
    warnings: list[Diag] = []
    warnings_grouped: dict[str, int] = {}


class SynthResult(Envelope):
    top: str = ""
    cells: int | None = None
    cell_types: dict[str, int] = {}
    area_um2: float | None = Field(None, description="Only with a liberty_ref")
    logic_depth: int | None = Field(None, description="Longest combinational path in generic gates (yosys ltp -noff)")
    warnings_grouped: dict[str, int] = {}
    netlist_ref: str | None = Field(None, description="<design_ref>:<path> of the written netlist")


class SimTest(_M):
    name: str
    status: Literal["pass", "fail"]
    time_ns: float | None = None
    msg: str = ""


class SignalTrace(_M):
    signal: str
    changes: list[tuple[float, str]] = Field(description="(time_ns, value) pairs inside the window")


class FirstFailure(_M):
    test: str
    time_ns: float | None
    msg: str
    signal_window: list[SignalTrace] = []


class SimResult(Envelope):
    simulator: str = ""
    seed: int | None = None
    status: Literal["pass", "fail", "error", "timeout"] = "error"
    tests: list[SimTest] = []
    first_failure: FirstFailure | None = None
    coverage: dict[str, int] | None = Field(None, description="{points, hit} from verilator --coverage")
    coverage_ref: str | None = None


class FailedProp(_M):
    name: str
    step: int | None
    location: str | None = None
    trace_ref: str | None = None


class FormalResult(Envelope):
    mode: str = ""
    depth: int | None = None
    status: Literal["pass", "fail", "unknown", "error", "timeout"] = "error"
    failed_props: list[FailedProp] = []


class EquivResult(Envelope):
    equivalent: bool | None = Field(None, description="True only if every $equiv cell is proven; unproven != disproven")
    proven: int = 0
    total: int = 0
    unproven: list[str] = []


class FlowResult(Envelope):
    stage: str = ""
    metrics: dict[str, float | int | str | None] = {}
    units: dict[str, str] = {}
    warnings_grouped: dict[str, int] = {}


class StaStage(_M):
    cell: str | None
    pin: str
    net: str | None = None
    delay_ps: float | None
    fanout: int | None = None


class StaPath(_M):
    slack_ps: float
    start: str
    end: str
    clock: str | None
    group: str | None
    arrival_ps: float | None = None
    required_ps: float | None = None
    stages: list[StaStage] = []


class StaResult(Envelope):
    stage: str = ""
    corner: str | None = None
    wns_ps: float | None = Field(None, description="Worst slack (may be positive)")
    tns_ps: float | None = None
    paths: list[StaPath] = []


class DrcViolation(_M):
    rule: str
    description: str | None = None
    layer: str | None = None
    bbox_um: list[float] | None = Field(None, description="[x0, y0, x1, y1] of the first marker")
    count: int


class DrcResult(Envelope):
    violations: list[DrcViolation] = []
    total: int = 0


class LvsResult(Envelope):
    match: bool | None = None
    mismatches: list[str] = []


class SnapshotResult(Envelope):
    files: int | None = None
    branch: str | None = None
    previous_ref: str | None = None


class DiffResult(Envelope):
    a_ref: str = ""
    b_ref: str = ""
    files_changed: dict[str, list[str]] = {}
    metric_deltas: dict[str, dict[str, Any]] = {}


class FileView(Envelope):
    path: str | None = None
    files: list[str] = Field([], description="Snapshot file listing when no path is given")
    lines: list[str] = []
    offset: int = 0
    next_offset: int | None = None
    total_lines: int | None = None


class LogPage(Envelope):
    page_of: str = ""
    pattern: str | None = None
    lines: list[str] = []
    offset: int = 0
    next_offset: int | None = None
    total_lines: int = 0


class ResultPage(Envelope):
    page_of: str = ""
    path: str = ""
    items: list[Any] = []
    value: Any = None
    offset: int = 0
    next_offset: int | None = None
    total: int | None = None


class ReplayResult(Envelope):
    original_run_id: str = ""
    match: bool | None = None
    original_digest: str | None = None
    replay_digest: str | None = None
    output_ref_match: bool | None = None


RESULT_MODELS = {
    "lint_run": LintResult, "synth_run": SynthResult, "sim_run": SimResult, "formal_run": FormalResult,
    "equiv_run": EquivResult, "flow_run_stage": FlowResult, "sta_report_paths": StaResult,
    "drc_run": DrcResult, "lvs_run": LvsResult, "snapshot_import": SnapshotResult,
    "snapshot_write": SnapshotResult, "snapshot_branch": SnapshotResult, "snapshot_restore": SnapshotResult,
    "snapshot_diff": DiffResult, "snapshot_read": FileView, "log_page": LogPage, "result_page": ResultPage,
    "replay_run": ReplayResult,
}

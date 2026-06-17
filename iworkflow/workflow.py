"""Declarative workflow engine — the "define your own workflow" door.

The MCP face can't accept a Python closure the way the harness's own Workflow
tool accepts a JS script, so a *dynamic* workflow is passed as **data**: a JSON
spec describing steps (agent / parallel / pipeline / loop) that this interpreter
runs onto the deterministic `Runner` primitives. Data, not code — safe to accept
from an agent across the MCP boundary, yet expressive enough for gate → fan →
audit shapes AND loops (count / dry / budget / fixed-N / agent-decided / vote).

Control flow stays deterministic Python (zero coordination tokens); only the
worker `agent()` calls spend the subscription. A spec proven useful can be saved
as a named recipe (see recipes.py) — the dynamic→confirmed→preset calcification.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .scheduler import AgentResult, Runner, log

# Schemas reused by built-in recipes + available to any spec by name. A spec can
# also inline a JSON Schema dict instead of a name.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["CONTINUE", "STOP"]},
        "reason": {"type": "string"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
}
# A supervisor agent inspects the accumulated run state at a checkpoint and emits
# this DECISION (data, not code): continue as-is, adjust the remaining plan (skip
# future steps / overlay params / inject new steps — each re-parsed under the same
# Limits, so no privilege escalation), or abort. The executor applies it; the
# control flow stays deterministic Python. See docs/design/supervisor.md.
SUPERVISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["continue", "adjust", "abort"]},
        "reason": {"type": "string"},
        "skip": {"type": "array", "items": {"type": "string"}},
        "set_params": {"type": "object"},
        "inject": {"type": "array", "items": {"type": "object"}},
    },
}
DEFAULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "decision": DECISION_SCHEMA, "supervision": SUPERVISION_SCHEMA}


class WorkflowError(Exception):
    """A spec is malformed or references something that does not exist."""


class WorkflowLimitError(WorkflowError):
    """A spec breaches a configured safety limit (resource bound or policy)."""


@dataclass(frozen=True)
class Limits:
    """Safety policy for a workflow run. Defaults are conservative because a spec
    can arrive from an untrusted agent over MCP: read-only sandbox only, no tool
    injection, and bounds that make a spec-authored fork-bomb impossible. Trusted
    callers (CLI/SDK) can widen these explicitly.

    `max_iterations` on a loop is the per-loop cap; `max_total_agent_calls` is the
    global cap across the whole run (the thing that actually bounds nested loops ×
    parallel × pipeline fan-out)."""

    allowed_sandboxes: frozenset[str] = frozenset({"read-only"})
    allow_tools: bool = False
    max_total_agent_calls: int = 64
    max_parallel_width: int = 16
    max_pipeline_items: int = 256
    max_loop_depth: int = 3
    max_loop_iterations: int = 100
    max_supervisions: int = 8        # times a supervisor may mutate the plan (anti-runaway)


# --------------------------------------------------------------------------
# templating — {{params.x}} / {{steps.gate.value.field}} / {{loop.collected}}
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _lookup(path: str, ctx: dict[str, Any]) -> Any:
    """Resolve a dotted path against the context — dict keys and list indices only
    (never attribute access, so a template can't reach into arbitrary objects)."""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Render templates in a string/dict/list.

    A string that is EXACTLY one `{{...}}` token resolves to the raw object (so
    `items: "{{loop.collected}}"` stays a list); otherwise tokens are stringified
    and substituted inline.
    """
    if isinstance(value, str):
        whole = _TOKEN.fullmatch(value.strip())
        if whole:
            return _lookup(whole.group(1), ctx)
        return _TOKEN.sub(lambda m: _stringify(_lookup(m.group(1), ctx)), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


# --------------------------------------------------------------------------
# `when` predicates — a declarative deviation guard (data, not code)
# --------------------------------------------------------------------------
# A leaf is {"path": "<dotted>", <op>: operand} with an optional "select" sub-path
# applied to each element when the path resolves to a list (so "any review's
# verdict is ISSUES" is one predicate). Leaves combine with {"all"|"any"|"not": …}.
# This evaluates the same accumulated ctx the prompt sees — no code, so it's safe
# to accept from an untrusted spec over MCP.
_LEAF_OPS = {"eq", "ne", "in", "nin", "gte", "lte", "gt", "lt", "contains",
             "truthy", "exists"}


def _validate_when(cond: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise WorkflowError("when nests too deep")
    if not isinstance(cond, dict):
        raise WorkflowError("when must be an object")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            raise WorkflowError(f"when.{key} must be a non-empty list")
        for sub in cond[key]:
            _validate_when(sub, depth=depth + 1)
        return
    if "not" in cond:
        _validate_when(cond["not"], depth=depth + 1)
        return
    if "path" not in cond:
        raise WorkflowError("when leaf needs a 'path' (or all/any/not)")
    ops = [k for k in cond if k in _LEAF_OPS]
    if len(ops) != 1:
        raise WorkflowError(
            f"when leaf needs exactly one operator of {sorted(_LEAF_OPS)}, got {ops}")


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _eval_leaf_op(cond: dict[str, Any], value: Any) -> bool:
    if "eq" in cond:
        return value == cond["eq"]
    if "ne" in cond:
        return value != cond["ne"]
    if "in" in cond:
        return isinstance(cond["in"], list) and value in cond["in"]
    if "nin" in cond:
        return isinstance(cond["nin"], list) and value not in cond["nin"]
    if "contains" in cond:
        if isinstance(value, (str, list, tuple, dict)):
            return cond["contains"] in value
        return False
    if "truthy" in cond:
        return _truthy(value) is bool(cond["truthy"])
    if "exists" in cond:
        return (value is not None) is bool(cond["exists"])
    a, b = _as_number(value), _as_number(cond.get("gte", cond.get("lte",
           cond.get("gt", cond.get("lt")))))
    if a is None or b is None:
        return False
    if "gte" in cond:
        return a >= b
    if "lte" in cond:
        return a <= b
    if "gt" in cond:
        return a > b
    return a < b                                  # "lt"


def _eval_when(cond: Any, ctx: dict[str, Any]) -> bool:
    """Evaluate a `when` predicate against the accumulated ctx. None → True
    (no guard)."""
    if cond is None:
        return True
    if "all" in cond:
        return all(_eval_when(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(_eval_when(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not _eval_when(cond["not"], ctx)
    root = _lookup(cond["path"], ctx)
    select = cond.get("select")
    elements = root if isinstance(root, list) else [root]
    # a leaf over a list is satisfied when ANY element satisfies it
    return any(_eval_leaf_op(cond, _lookup(select, el) if select else el)
               for el in elements)


# --------------------------------------------------------------------------
# parsed spec
# --------------------------------------------------------------------------
@dataclass
class AgentSpec:
    id: str
    prompt: str
    schema: str | dict[str, Any] | None = None
    prefer: list[str | dict[str, Any]] | None = None
    model: str | None = None
    models: dict[str, str] | None = None
    role: str | None = None
    sandbox: str = "read-only"
    tools: list[str] | None = None
    gate: dict[str, Any] | None = None


@dataclass
class Until:
    kind: str                       # times | count | dry | budget | agent | vote
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    id: str
    kind: str                       # agent | parallel | pipeline | loop | supervisor
    needs: list[str] = field(default_factory=list)
    agent: AgentSpec | None = None             # kind == agent
    agents: list[AgentSpec] = field(default_factory=list)   # kind == parallel
    items: Any = None                          # kind == pipeline (template → list)
    stages: list[AgentSpec] = field(default_factory=list)   # kind == pipeline
    body: list["Step"] = field(default_factory=list)        # kind == loop
    until: Until | None = None                 # kind == loop
    max_iterations: int = 0                    # kind == loop (mandatory)
    collect: dict[str, Any] | None = None      # kind == loop
    supervisor: AgentSpec | None = None        # kind == supervisor (the coordinator agent)
    watch: list[str] | None = None             # kind == supervisor (steps to expose; None=all)
    when: dict[str, Any] | None = None         # kind == supervisor (deviation guard; None=always)


@dataclass
class WorkflowSpec:
    steps: list[Step]
    name: str | None = None
    description: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)

    @staticmethod
    def parse(spec: dict[str, Any], limits: Limits | None = None) -> "WorkflowSpec":
        limits = limits or Limits()
        if not isinstance(spec, dict):
            raise WorkflowError("spec must be an object")
        raw_steps = spec.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkflowError("spec.steps must be a non-empty list")
        seen: set[str] = set()
        steps = [_parse_step(s, seen, top=True, limits=limits, depth=0) for s in raw_steps]
        return WorkflowSpec(
            steps=steps,
            name=spec.get("name"),
            description=spec.get("description"),
            params=spec.get("params") or {},
            output=spec.get("output"),
            schemas={**DEFAULT_SCHEMAS, **(spec.get("schemas") or {})},
            limits=limits,
        )


_VALID_KINDS = {"agent", "parallel", "pipeline", "loop", "supervisor"}
_VALID_UNTIL = {"times", "count", "dry", "budget", "agent", "vote"}


def _parse_agent(d: dict[str, Any], fallback_id: str, limits: Limits) -> AgentSpec:
    if not isinstance(d, dict):
        raise WorkflowError("agent step must be an object")
    if "prompt" not in d:
        raise WorkflowError(f"agent step {d.get('id', fallback_id)!r} needs a prompt")
    aid = str(d.get("id", fallback_id))
    # A spec may arrive from an untrusted agent over MCP: never let it pick a
    # privileged sandbox (→ `codex exec --sandbox <value>`) or inject tools.
    sandbox = d.get("sandbox", "read-only")
    if sandbox not in limits.allowed_sandboxes:
        raise WorkflowLimitError(
            f"agent {aid!r}: sandbox {sandbox!r} not permitted "
            f"(allowed: {sorted(limits.allowed_sandboxes)})")
    tools = d.get("tools")
    if tools and not limits.allow_tools:
        raise WorkflowLimitError(
            f"agent {aid!r}: tool injection is disabled by policy")
    return AgentSpec(
        id=aid,
        prompt=d["prompt"],
        schema=d.get("schema"),
        prefer=d.get("prefer"),
        model=d.get("model"),
        models=d.get("models"),
        role=d.get("role"),
        sandbox=sandbox,
        tools=tools,
        gate=d.get("gate"),
    )


def _parse_until(d: Any) -> Until:
    if not isinstance(d, dict) or len(d) != 1:
        raise WorkflowError("loop.until must be an object with exactly one kind")
    kind, params = next(iter(d.items()))
    if kind not in _VALID_UNTIL:
        raise WorkflowError(f"unknown loop.until kind {kind!r}; valid: {sorted(_VALID_UNTIL)}")
    if kind == "times":
        n = int(params)
        if n < 1:
            raise WorkflowError("loop.until.times must be >= 1")
        return Until(kind=kind, params={"n": n})
    if not isinstance(params, dict):
        raise WorkflowError(f"loop.until.{kind} must be an object")
    if kind == "count" and int(params.get("target", 0)) < 1:
        raise WorkflowError("loop.until.count.target must be >= 1")
    if kind == "dry" and int(params.get("rounds", 0)) < 1:
        raise WorkflowError("loop.until.dry.rounds must be >= 1")
    if kind == "budget" and int(params.get("output_tokens", 0)) < 1:
        raise WorkflowError("loop.until.budget.output_tokens must be >= 1")
    if kind in {"agent", "vote"} and "prompt" not in params:
        raise WorkflowError(f"loop.until.{kind} needs a prompt")
    if kind == "vote":
        lenses = params.get("lenses")
        count = len(lenses) if lenses else int(params.get("count", 3))
        if count < 1:
            raise WorkflowError("loop.until.vote.count must be >= 1")
        threshold = int(params.get("threshold", count // 2 + 1))
        if not 1 <= threshold <= count:                 # threshold=0 would stop with zero STOP votes
            raise WorkflowError(f"loop.until.vote.threshold must be in [1, {count}]")
    return Until(kind=kind, params=params)


def _parse_step(d: dict[str, Any], seen: set[str], *, top: bool,
                limits: Limits, depth: int) -> Step:
    if not isinstance(d, dict):
        raise WorkflowError("each step must be an object")
    kind = d.get("kind")
    if kind not in _VALID_KINDS:
        raise WorkflowError(f"step kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")
    sid = d.get("id")
    if not sid or not isinstance(sid, str):
        raise WorkflowError(f"every step needs a string id (kind={kind})")
    if sid in seen:
        raise WorkflowError(f"duplicate step id {sid!r}")
    seen.add(sid)
    needs = d.get("needs") or []
    for dep in needs:
        if dep not in seen:
            raise WorkflowError(f"step {sid!r} needs {dep!r}, which is not a prior step")
    step = Step(id=sid, kind=kind, needs=list(needs))

    if kind == "agent":
        step.agent = _parse_agent(d, sid, limits)
    elif kind == "parallel":
        raw = d.get("agents")
        if not isinstance(raw, list) or not raw:
            raise WorkflowError(f"parallel step {sid!r} needs a non-empty agents list")
        if len(raw) > limits.max_parallel_width:
            raise WorkflowLimitError(
                f"parallel step {sid!r} fans out {len(raw)} agents > "
                f"max_parallel_width={limits.max_parallel_width}")
        step.agents = [_parse_agent(a, f"{sid}.{i}", limits) for i, a in enumerate(raw)]
    elif kind == "pipeline":
        if "items" not in d:
            raise WorkflowError(f"pipeline step {sid!r} needs items")
        raw = d.get("stages")
        if not isinstance(raw, list) or not raw:
            raise WorkflowError(f"pipeline step {sid!r} needs a non-empty stages list")
        step.items = d["items"]
        step.stages = [_parse_agent(a, f"{sid}.s{i}", limits) for i, a in enumerate(raw)]
    elif kind == "loop":
        if depth >= limits.max_loop_depth:
            raise WorkflowLimitError(
                f"loop step {sid!r} nests deeper than max_loop_depth={limits.max_loop_depth}")
        raw = d.get("body")
        if not isinstance(raw, list) or not raw:
            raise WorkflowError(f"loop step {sid!r} needs a non-empty body")
        inner: set[str] = set()
        step.body = [_parse_step(b, inner, top=False, limits=limits, depth=depth + 1)
                     for b in raw]
        step.until = _parse_until(d.get("until"))
        step.max_iterations = int(d.get("max_iterations", 0))
        if step.max_iterations <= 0:
            raise WorkflowError(f"loop step {sid!r} needs max_iterations > 0 (anti-runaway)")
        if step.max_iterations > limits.max_loop_iterations:
            raise WorkflowLimitError(
                f"loop step {sid!r} max_iterations={step.max_iterations} > "
                f"max_loop_iterations={limits.max_loop_iterations}")
        if step.until.kind == "times" and step.until.params["n"] > step.max_iterations:
            raise WorkflowError(
                f"loop step {sid!r}: until.times={step.until.params['n']} exceeds "
                f"max_iterations={step.max_iterations}")
        step.collect = d.get("collect")
        if step.until.kind in {"count", "dry"} and not step.collect:
            raise WorkflowError(
                f"loop step {sid!r} with until.{step.until.kind} needs a collect block")
    elif kind == "supervisor":
        # The coordinator runs over the macro plan, so it is top-level only (a
        # supervisor inside a loop body would supervise loop internals, not the run).
        if not top:
            raise WorkflowError(f"supervisor step {sid!r} must be a top-level step")
        watch = d.get("watch")
        if watch is not None and not isinstance(watch, list):
            raise WorkflowError(f"supervisor step {sid!r}: watch must be a list of step ids")
        # reuse the agent parser (prompt/prefer/role/sandbox/tools gating) — default
        # its schema to the built-in `supervision` decision shape.
        step.supervisor = _parse_agent({"schema": "supervision", **d}, sid, limits)
        step.watch = watch
        # optional deviation guard: the coordinator only fires when `when` is true,
        # so the common path spends zero coordinator tokens. Validate it now.
        when = d.get("when")
        if when is not None:
            _validate_when(when)
        step.when = when
    return step


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------
async def run_spec(runner: Runner, spec: dict[str, Any],
                   params: dict[str, Any] | None = None,
                   limits: Limits | None = None) -> dict[str, Any]:
    """Parse + execute a declarative workflow spec, returning a result bundle.

    `limits` is the safety policy (sandbox allowlist + resource bounds). It
    defaults to the conservative `Limits()` — correct for specs that arrive from
    an untrusted agent over MCP. Trusted callers can widen it."""
    wf = WorkflowSpec.parse(spec, limits)
    return await _Executor(runner, wf, params or {}).run()


class _Abort(Exception):
    def __init__(self, step_id: str) -> None:
        self.step_id = step_id


class _Executor:
    def __init__(self, runner: Runner, wf: WorkflowSpec, params: dict[str, Any]) -> None:
        self.runner = runner
        self.wf = wf
        self.limits = wf.limits
        self.schemas = wf.schemas
        self.params = {**wf.params, **params}    # spec defaults, caller overrides
        self.ctx: dict[str, Any] = {"params": self.params, "steps": {}}
        self.calls = 0                           # global agent-call budget (anti fork-bomb)
        # a supervisor step may mutate the REMAINING plan (skip/inject), so the run
        # loop iterates a mutable copy by index rather than the frozen spec list.
        self.plan: list[Step] = list(self.wf.steps)
        self._ids: set[str] = {s.id for s in self.wf.steps}   # for inject id-collision checks
        self.supervisions = 0                    # plan mutations so far (capped by Limits)
        # durable resume at the STEP boundary: a completed top-level step (a whole
        # loop included) is journaled, so a resumed run (same run_id) short-circuits
        # it to its stored result instead of replaying. An in-flight loop still
        # resumes mid-flight via its body's agent-label cache in the scheduler ledger.
        self._steps_path = runner.ledger.run_dir / "wf-steps.json"
        self._completed: dict[str, Any] = self._load_steps()
        for sid, result in self._completed.items():
            self.ctx["steps"][sid] = result

    def _load_steps(self) -> dict[str, Any]:
        if not self._steps_path.exists():
            return {}
        try:
            data = json.loads(self._steps_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _persist_steps(self) -> None:
        tmp = self._steps_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._completed, default=str), encoding="utf-8")
        os.replace(tmp, self._steps_path)

    async def run(self) -> dict[str, Any]:
        status, aborted_at = "DONE", None
        try:
            i = 0
            while i < len(self.plan):                      # plan may grow/shrink via supervisor
                step = self.plan[i]
                if step.id in self._completed:            # journaled on a prior run
                    result = self._completed[step.id]
                    self.ctx["steps"][step.id] = result
                    if step.kind == "supervisor":
                        # replay the supervisor's recorded decision so the resumed plan
                        # is re-mutated identically — WITHOUT calling the agent again.
                        self._apply_supervision(result.get("value") or {}, i, replay=True)
                    else:
                        log(f"RESUMED step {step.id} (journaled, 0 agents)")
                    i += 1
                    continue
                if step.kind == "supervisor":
                    result = await self._exec_supervisor(step, i)
                else:
                    result = await self._exec_step(step, self.ctx, step.id)
                self.ctx["steps"][step.id] = result
                self._completed[step.id] = result
                self._persist_steps()
                i += 1
        except _Abort as a:
            status, aborted_at = "ABORTED", a.step_id
        out = render(self.wf.output, self.ctx) if self.wf.output is not None else None
        bundle = {
            "status": status,
            "name": self.wf.name,
            "output": out,
            "steps": {sid: r.get("value") for sid, r in self.ctx["steps"].items()},
        }
        if aborted_at:
            bundle["aborted_at"] = aborted_at
        return bundle

    # --- schema / agent helpers ------------------------------------------
    def _schema(self, schema: str | dict[str, Any] | None) -> dict[str, Any] | None:
        if schema is None or isinstance(schema, dict):
            return schema
        resolved = self.schemas.get(schema)
        if resolved is None:
            raise WorkflowError(f"unknown schema name {schema!r}")
        return resolved

    async def _agent_call(self, a: AgentSpec, ctx: dict[str, Any], label: str) -> AgentResult:
        self.calls += 1
        if self.calls > self.limits.max_total_agent_calls:
            raise WorkflowLimitError(
                f"workflow exceeded max_total_agent_calls={self.limits.max_total_agent_calls}")
        return await self.runner.agent(
            render(a.prompt, ctx),
            label=label,
            schema=self._schema(a.schema),
            prefer=a.prefer,
            model=a.model,
            models=a.models,
            role=a.role,
            sandbox=a.sandbox,
            tools=a.tools,
        )

    @staticmethod
    def _result(res: AgentResult, **extra: Any) -> dict[str, Any]:
        return {"value": res.value, "provider": res.provider, "ok": res.ok, **extra}

    # --- dispatch ---------------------------------------------------------
    async def _exec_step(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        if step.kind == "agent":
            return await self._exec_agent(step, ctx, label)
        if step.kind == "parallel":
            return await self._exec_parallel(step, ctx, label)
        if step.kind == "pipeline":
            return await self._exec_pipeline(step, ctx, label)
        if step.kind == "loop":
            return await self._exec_loop(step, ctx, label)
        # supervisor is top-level only (run() handles it); anything else is a bug.
        raise WorkflowError(f"cannot execute step kind {step.kind!r} here")

    async def _exec_agent(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        a = step.agent
        assert a is not None
        res = await self._agent_call(a, ctx, label)
        out = self._result(res, kind="agent")
        if a.gate and res.ok:
            field_name = a.gate.get("field")
            value = res.value.get(field_name) if isinstance(res.value, dict) and field_name \
                else res.value
            abort_on = a.gate.get("abort_on")
            abort_set = {abort_on} if isinstance(abort_on, str) else set(abort_on or [])
            if value in abort_set:
                out["aborted"] = True
                raise _Abort(step.id)
        return out

    async def _exec_parallel(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        def thunk(a: AgentSpec):
            return lambda: self._agent_call(a, ctx, f"{label}:{a.id}")

        results = await self.runner.parallel([thunk(a) for a in step.agents])
        value = [self._result(r, id=a.id) for a, r in zip(step.agents, results)]
        return {"kind": "parallel", "value": value, "ok": all(r.ok for r in results)}

    async def _exec_pipeline(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        items = render(step.items, ctx)
        if not isinstance(items, list):
            raise WorkflowError(f"pipeline step {step.id!r} items did not resolve to a list")
        if len(items) > self.limits.max_pipeline_items:
            raise WorkflowLimitError(
                f"pipeline step {step.id!r} has {len(items)} items > "
                f"max_pipeline_items={self.limits.max_pipeline_items}")

        def make_stage(a: AgentSpec):
            async def run(prev: Any, item: Any, idx: int) -> Any:
                sctx = {**ctx, "item": item, "prev": prev, "index": idx}
                res = await self._agent_call(a, sctx, f"{label}:{a.id}#{idx}")
                return self._result(res)
            return run

        out = await self.runner.pipeline(items, *[make_stage(a) for a in step.stages])
        return {"kind": "pipeline", "value": out, "ok": all(x is not None for x in out)}

    # --- the supervisor (adaptive re-planning) ---------------------------
    def _supervisor_state(self, step: Step) -> dict[str, Any]:
        """The accumulated run state handed to the coordinator. `watch` (if set)
        filters which prior steps' values are exposed; `remaining` lists the steps
        still ahead so the coordinator can reason about what's left to do."""
        watch = step.watch
        steps_state = {sid: r.get("value") for sid, r in self.ctx["steps"].items()
                       if watch is None or sid in watch}
        done = set(self.ctx["steps"])
        remaining = [s.id for s in self.plan
                     if s.id not in done and s.kind != "supervisor"]
        return {"steps": steps_state, "remaining": remaining, "params": dict(self.params)}

    async def _exec_supervisor(self, step: Step, index: int) -> dict[str, Any]:
        a = step.supervisor
        assert a is not None
        sctx = {**self.ctx, "supervisor": self._supervisor_state(step)}
        # deviation guard: if `when` is false, the coordinator never fires — no agent
        # call, no plan mutation. The common (on-track) path is free.
        if step.when is not None and not _eval_when(step.when, sctx):
            log(f"SUPERVISOR {step.id}: when=false → skipped (0 agents)")
            return {"kind": "supervisor", "ok": True,
                    "value": {"action": "continue", "skipped_guard": True},
                    "applied": {"skipped": [], "injected": [], "params": [], "errors": []}}
        res = await self._agent_call(a, sctx, step.id)
        # a failed/garbage decision degrades to "continue" — never silently mutate.
        decision = res.value if (res.ok and isinstance(res.value, dict)) else {"action": "continue"}
        applied = self._apply_supervision(decision, index, replay=False)
        out = {"kind": "supervisor", "value": decision, "applied": applied,
               "provider": res.provider, "ok": res.ok}
        if decision.get("action") == "abort":
            out["aborted"] = True
            # persist the decision BEFORE aborting so the bundle/resume keep the reason.
            self.ctx["steps"][step.id] = out
            self._completed[step.id] = out
            self._persist_steps()
            raise _Abort(step.id)
        return out

    def _apply_supervision(self, decision: dict[str, Any], index: int, *,
                           replay: bool) -> dict[str, Any]:
        """Apply a coordinator decision to the REMAINING plan (steps after `index`).
        Only `adjust` mutates; `continue`/`abort` are no-ops here. Deterministic, so
        replay on resume reproduces the same plan from the journaled decision."""
        applied: dict[str, Any] = {"skipped": [], "injected": [], "params": [], "errors": []}
        if decision.get("action") != "adjust":
            return applied
        self.supervisions += 1
        if self.supervisions > self.limits.max_supervisions:
            raise WorkflowLimitError(
                f"workflow exceeded max_supervisions={self.limits.max_supervisions}")

        set_params = decision.get("set_params")
        if isinstance(set_params, dict) and set_params:
            self.params.update(set_params)          # same object as ctx["params"] → future renders see it
            applied["params"] = sorted(set_params.keys())

        skip = decision.get("skip") or []
        if skip:
            done = set(self.ctx["steps"])
            future = {self.plan[j].id for j in range(index + 1, len(self.plan))}
            to_skip = {s for s in skip if s in future and s not in done}
            if to_skip:                              # never touch the past, only the tail
                self.plan = [s for k, s in enumerate(self.plan)
                             if not (k > index and s.id in to_skip)]
                applied["skipped"] = sorted(to_skip)

        new_steps: list[Step] = []
        for raw in (decision.get("inject") or []):
            trial_ids = set(self._ids)               # don't pollute ids with a failed parse
            try:                                     # same Limits → no sandbox/tool escalation
                st = _parse_step(raw, trial_ids, top=True, limits=self.limits, depth=0)
            except WorkflowError as e:               # malformed inject: drop it, keep the run
                applied["errors"].append(str(e)[:160])
                continue
            self._ids = trial_ids                    # commit only a fully-parsed step's id
            new_steps.append(st)
        if new_steps:
            self.plan[index + 1:index + 1] = new_steps   # run right after this supervisor
            applied["injected"] = [s.id for s in new_steps]
        return applied

    # --- the loop ---------------------------------------------------------
    async def _exec_loop(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        until = step.until
        assert until is not None
        collected: list[Any] = []
        seen: set[Any] = set()
        iteration = 0
        dry_rounds = 0
        decision: Any = None
        # budget is scoped to THIS loop's own ledger labels (not the whole run, so a
        # concurrent sibling can't inflate it) and is a DELTA from entry. A COMPLETED
        # loop is journaled at the step boundary (run() short-circuits it on resume);
        # only an IN-FLIGHT loop re-enters here, replaying cached body calls at zero
        # cost and spending whatever budget is left. max_iterations is the hard cap.
        base_tokens = self._loop_tokens_spent(label)
        body_results: dict[str, dict[str, Any]] = {}
        stop_reason = "max_iterations"

        loop_ctx = {**ctx, "loop": {
            "iteration": 0, "collected": collected, "decision": None, "last": body_results}}

        while iteration < step.max_iterations:
            if self._mechanical_stop(until, collected, dry_rounds, base_tokens, iteration, label):
                stop_reason = until.kind
                break

            body_results = {}                         # fresh per iteration — no stale leak
            loop_ctx["loop"]["iteration"] = iteration
            loop_ctx["loop"]["decision"] = decision
            loop_ctx["loop"]["last"] = body_results
            aborted = False
            for bstep in step.body:
                try:
                    body_results[bstep.id] = await self._exec_step(
                        bstep, loop_ctx, f"{label}#{iteration}/{bstep.id}")
                except _Abort:
                    aborted = True
                    break

            iteration += 1
            if aborted:                               # do NOT collect a half-run iteration
                stop_reason = "aborted"
                break

            added = self._collect(step, body_results, collected, seen)
            dry_rounds = dry_rounds + 1 if added == 0 else 0

            if until.kind in {"agent", "vote"}:
                decision, stop = await self._decide(until, loop_ctx, label, iteration)
                loop_ctx["loop"]["decision"] = decision
                if stop:
                    stop_reason = "decider"
                    break

        log(f"LOOP {label}: {iteration} iteration(s), stop={stop_reason}, "
            f"collected={len(collected)}")
        return {
            "kind": "loop",
            "value": collected,
            "iterations": iteration,
            "stop_reason": stop_reason,
            "decision": decision,
            "ok": True,
        }

    def _mechanical_stop(self, until: Until, collected: list[Any], dry_rounds: int,
                         base_tokens: int, iteration: int, label: str) -> bool:
        if until.kind == "times":
            return iteration >= int(until.params.get("n", 0))
        if until.kind == "count":
            return len(collected) >= int(until.params.get("target", 0))
        if until.kind == "dry":
            return dry_rounds >= int(until.params.get("rounds", 1))
        if until.kind == "budget":
            spent = self._loop_tokens_spent(label) - base_tokens
            return spent >= int(until.params.get("output_tokens", 0))
        return False

    async def _decide(self, until: Until, loop_ctx: dict[str, Any],
                      label: str, iteration: int) -> tuple[Any, bool]:
        p = until.params
        field_name = p.get("field", "verdict")
        stop_when = p.get("stop_when", "STOP")
        stop_set = {stop_when} if isinstance(stop_when, str) else set(stop_when)
        schema = p.get("schema", "decision")

        if until.kind == "agent":
            res = await self._agent_call(
                AgentSpec(id="decide", prompt=p["prompt"], schema=schema,
                          prefer=p.get("prefer"), model=p.get("model"),
                          models=p.get("models"), role=p.get("role")),
                loop_ctx, f"{label}#decide{iteration}")
            verdict = res.value.get(field_name) if isinstance(res.value, dict) else None
            return res.value, (res.ok and verdict in stop_set)

        # vote: N deciders (optionally perspective-diverse via `lenses`), majority STOP
        lenses = p.get("lenses")
        count = len(lenses) if lenses else int(p.get("count", 3))
        threshold = int(p.get("threshold", count // 2 + 1))
        prompt = p["prompt"]

        def voter(i: int):
            lens = f"\n\nJudge specifically through the {lenses[i]} lens." if lenses else ""
            return lambda: self._agent_call(
                AgentSpec(id=f"vote{i}", prompt=prompt + lens, schema=schema,
                          prefer=p.get("prefer"), model=p.get("model"),
                          models=p.get("models")),
                loop_ctx, f"{label}#vote{iteration}.{i}")

        results = await self.runner.parallel([voter(i) for i in range(count)])
        verdicts = [r.value.get(field_name) if (r.ok and isinstance(r.value, dict)) else None
                    for r in results]
        stops = sum(1 for v in verdicts if v in stop_set)
        return {"verdicts": verdicts, "stops": stops, "threshold": threshold}, stops >= threshold

    # --- accumulation -----------------------------------------------------
    def _collect(self, step: Step, body_results: dict[str, dict[str, Any]],
                 collected: list[Any], seen: set[Any]) -> int:
        spec = step.collect
        if not spec:
            return 0
        src = body_results.get(spec["from"])
        if not src:
            return 0
        where = spec.get("where")
        dedup = spec.get("dedup_by")
        added = 0
        for item in self._extract_items(src, spec.get("path")):
            if where is not None and not _truthy(
                    render(where, {**self.ctx, "item": item})):
                continue
            if dedup is not None:
                key = item.get(dedup) if isinstance(item, dict) else item
                key = key if isinstance(key, (str, int, float, bool, type(None))) \
                    else json.dumps(key, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
            collected.append(item)
            added += 1
        return added

    @staticmethod
    def _extract_items(src: dict[str, Any], path: str | None) -> list[Any]:
        value = src.get("value")
        elements = [a.get("value") for a in value] if src.get("kind") == "parallel" \
            and isinstance(value, list) else [value]
        items: list[Any] = []
        for el in elements:
            target = el.get(path) if (path and isinstance(el, dict)) else el
            if isinstance(target, list):
                items.extend(target)
            elif target is not None:
                items.append(target)
        return items

    def _loop_tokens_spent(self, label: str) -> int:
        """Output tokens recorded for THIS loop's labels (prefix `<label>#`).
        Scoped to the loop (a concurrent sibling can't inflate it) and read from
        the ledger (so an original run's spend still counts after a resume)."""
        prefix = f"{label}#"
        total = 0
        for lbl, rec in self.runner.ledger.load_done().items():
            if lbl.startswith(prefix):
                total += rec.get("output_tokens") or 0
        return total

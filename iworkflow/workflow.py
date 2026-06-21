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
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from .scheduler import AgentResult, Runner, log
from .minijsonschema import validate as validate_json_schema

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


_WORKTREE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _worktree_component(value: str) -> str:
    cleaned = _WORKTREE_SAFE.sub("_", str(value)).strip("._-")
    return cleaned or "unnamed"


def _is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _worktree_path(run_id: str, step_id: str, agent_id: str) -> Path:
    return Path(tempfile.gettempdir()) / (
        "iwf-"
        f"{_worktree_component(run_id)}-"
        f"{_worktree_component(step_id)}-"
        f"{_worktree_component(agent_id)}"
    )


async def _git_exec(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr


def _git_error(action: str, stderr: str, stdout: str = "") -> WorkflowError:
    detail = (stderr or stdout).strip() or "git command failed"
    return WorkflowError(f"{action} failed: {detail}")


async def _create_worktree(base_cwd: Path, wt_path: Path, branch: str) -> None:
    del branch  # Worktrees are detached; the deterministic path carries identity.
    if wt_path.exists():
        await _remove_worktree(base_cwd, wt_path)
        if wt_path.exists():
            shutil.rmtree(wt_path)
    code, stdout, stderr = await _git_exec(
        base_cwd,
        "worktree",
        "add",
        "--detach",
        str(wt_path),
    )
    if code != 0:
        raise _git_error("git worktree add", stderr, stdout)


async def _remove_worktree(base_cwd: Path, wt_path: Path) -> None:
    code, stdout, stderr = await _git_exec(
        base_cwd,
        "worktree",
        "remove",
        "--force",
        str(wt_path),
    )
    if code != 0 and wt_path.exists():
        detail = (stderr or stdout).strip() or "unknown error"
        log(f"WARN     git worktree remove failed for {wt_path}: {detail}")


async def _has_staged_changes(repo: Path) -> bool:
    code, stdout, stderr = await _git_exec(repo, "diff", "--cached", "--quiet")
    if code == 0:
        return False
    if code == 1:
        return True
    raise _git_error("git diff --cached", stderr, stdout)


async def _consolidate_worktree(base_cwd: Path, wt_path: Path) -> None:
    code, stdout, stderr = await _git_exec(
        wt_path,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    if code != 0:
        raise _git_error("git status", stderr, stdout)
    if not stdout.strip():
        return

    code, stdout, stderr = await _git_exec(wt_path, "add", "-A")
    if code != 0:
        raise _git_error("git add", stderr, stdout)
    if not await _has_staged_changes(wt_path):
        return

    code, stdout, stderr = await _git_exec(
        wt_path,
        "commit",
        "-m",
        "iworkflow isolated agent changes",
    )
    if code != 0:
        raise _git_error("git commit", stderr, stdout)

    code, stdout, stderr = await _git_exec(wt_path, "rev-parse", "HEAD")
    if code != 0:
        raise _git_error("git rev-parse HEAD", stderr, stdout)
    head = stdout.strip()

    code, stdout, stderr = await _git_exec(base_cwd, "merge", "--squash", head)
    if code != 0:
        raise _git_error("git merge --squash", stderr, stdout)
    if await _has_staged_changes(base_cwd):
        code, stdout, stderr = await _git_exec(base_cwd, "commit", "--no-edit")
        if code != 0:
            raise _git_error("git commit --no-edit", stderr, stdout)


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
    write_paths: list[Any] = field(default_factory=list)
    gate: dict[str, Any] | None = None
    timeout_s: int | None = None
    heartbeat_interval_s: int | None = None
    required: bool = True


@dataclass
class Until:
    kind: str                       # times | count | dry | budget | agent | vote
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    id: str
    kind: str                       # agent | parallel | pipeline | loop | supervisor | command | checkpoint
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
    when: dict[str, Any] | None = None         # top-level conditional guard; None=always
    command: str | list[str] | None = None                  # kind == command
    cwd: str | None = None                                  # kind == command
    env: dict[str, str] | None = None                       # kind == command
    timeout_s: int | None = None                            # kind == command
    gate: dict[str, Any] | None = None                      # kind == command
    checkpoint: dict[str, Any] | None = None                # kind == checkpoint


@dataclass
class WorkflowSpec:
    steps: list[Step]
    name: str | None = None
    description: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    artifacts: list[Any] = field(default_factory=list)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    execution: dict[str, Any] = field(default_factory=dict)

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
            artifacts=spec.get("artifacts") or [],
            schemas={**DEFAULT_SCHEMAS, **(spec.get("schemas") or {})},
            limits=limits,
            execution=spec.get("execution") or {},
        )


_VALID_KINDS = {"agent", "parallel", "pipeline", "loop", "supervisor", "command", "checkpoint"}
_VALID_UNTIL = {"times", "count", "dry", "budget", "agent", "vote"}
_VALID_CHECKPOINT_MODES = {"approval", "input", "confirm"}


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
        write_paths=d.get("write_paths") or [],
        gate=d.get("gate"),
        timeout_s=d.get("timeout_s"),
        heartbeat_interval_s=d.get("heartbeat_interval_s"),
        required=bool(d.get("required", True)),
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
    elif kind == "command":
        cmd = d.get("command")
        if not cmd:
            raise WorkflowError(f"command step {sid!r} needs a 'command'")
        if not isinstance(cmd, (str, list)):
            raise WorkflowError(f"command step {sid!r}: command must be a string or list of strings")
        step.command = cmd
        step.cwd = d.get("cwd")
        step.env = d.get("env")
        step.timeout_s = d.get("timeout_s")
        step.gate = d.get("gate")
    elif kind == "checkpoint":
        mode = d.get("mode", "approval")
        if mode not in _VALID_CHECKPOINT_MODES:
            raise WorkflowError(
                f"checkpoint step {sid!r}: mode must be one of {sorted(_VALID_CHECKPOINT_MODES)}")
        title = d.get("title")
        prompt = d.get("prompt", title)
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError(f"checkpoint step {sid!r} needs a non-empty prompt or title")
        if title is not None and not isinstance(title, str):
            raise WorkflowError(f"checkpoint step {sid!r}: title must be a string")
        artifact = d.get("artifact")
        if artifact is not None and not isinstance(artifact, str):
            raise WorkflowError(f"checkpoint step {sid!r}: artifact must be a string path")
        output = d.get("output")
        if not isinstance(output, str) or not output.strip():
            raise WorkflowError(f"checkpoint step {sid!r} needs a non-empty output path")
        schema = d.get("schema")
        if mode == "input" and schema is None:
            raise WorkflowError(f"checkpoint step {sid!r} with mode='input' needs a schema")
        if schema is not None and not isinstance(schema, (str, dict)):
            raise WorkflowError(f"checkpoint step {sid!r}: schema must be a name or object")
        step.checkpoint = {
            "mode": mode,
            "title": title or prompt,
            "prompt": prompt,
            "artifact": artifact,
            "schema": schema,
            "output": output,
        }
    when = d.get("when")
    if top and when is not None:
        _validate_when(when)
        step.when = when
    return step


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------
async def run_spec(runner: Runner, spec: dict[str, Any],
                   params: dict[str, Any] | None = None,
                   limits: Limits | None = None,
                   preflight_checked: bool = False) -> dict[str, Any]:
    """Parse + execute a declarative workflow spec, returning a result bundle.

    `limits` is the safety policy (sandbox allowlist + resource bounds). It
    defaults to the conservative `Limits()` — correct for specs that arrive from
    an untrusted agent over MCP. Trusted callers can widen it."""
    wf = WorkflowSpec.parse(spec, limits)
    return await _Executor(runner, wf, params or {}, preflight_checked=preflight_checked).run()


class _Abort(Exception):
    def __init__(self, step_id: str) -> None:
        self.step_id = step_id


class _Pause(Exception):
    def __init__(self, step_id: str, request: dict[str, Any]) -> None:
        self.step_id = step_id
        self.request = request


def check_preflight(
    execution: dict[str, Any],
    cwd: str | None,
    *,
    ignore_paths: list[str] | tuple[str, ...] = (),
) -> None:
    import shutil
    import subprocess

    checked_cwd = os.path.abspath(cwd or os.getcwd())

    def fail(message: str) -> None:
        raise WorkflowError(f"pre-flight check failed in {checked_cwd}: {message}")

    if not os.path.isdir(checked_cwd):
        fail("working directory does not exist")

    # 1. Check gh requirement
    if execution.get("gh_required"):
        if shutil.which("gh") is None:
            fail("GitHub CLI (gh) is required but not installed.")
        try:
            res = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                cwd=checked_cwd,
                check=False
            )
        except FileNotFoundError:
            fail("GitHub CLI (gh) is required but not installed.")
        if res.returncode != 0:
            fail("GitHub CLI (gh) is not authenticated. Please run 'gh auth login' first.")

    # 2. Check worktree requirements (requires git clean state)
    worktree = execution.get("worktree")
    if worktree:
        git_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            cwd=checked_cwd,
            check=False
        )
        if git_check.returncode != 0:
            fail("Workflow execution requires a git repository but none was found.")

        pathspecs = [".", ":(exclude).iworkflow"]
        for ignored in ignore_paths:
            ignored_abs = os.path.abspath(ignored)
            try:
                rel = os.path.relpath(ignored_abs, checked_cwd)
            except ValueError:
                continue
            if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
                continue
            pathspecs.append(f":(exclude){rel}")
            pathspecs.append(f":(exclude){rel.rstrip(os.sep)}/")

        if execution.get("git_clean_required"):
            status_check = subprocess.run(
                ["git", "status", "--porcelain", "--", *pathspecs],
                capture_output=True,
                text=True,
                cwd=checked_cwd,
                check=False
            )
            if status_check.returncode == 0:
                dirty = []
                for line in status_check.stdout.splitlines():
                    if line.startswith("??"):
                        continue
                    entry = line[3:] if len(line) > 3 else line
                    if entry:
                        dirty.append(entry)
                if dirty:
                    preview = ", ".join(dirty[:10])
                    if len(dirty) > 10:
                        preview += f", … and {len(dirty) - 10} more"
                    detail = f" Dirty paths: {preview}." if preview else ""
                    fail(
                        "Git repository has uncommitted changes outside the iworkflow journal. "
                        "Please stash or commit them first." + detail
                    )


class _Executor:
    def __init__(self, runner: Runner, wf: WorkflowSpec, params: dict[str, Any], *,
                 preflight_checked: bool = False) -> None:
        self.runner = runner
        self.wf = wf
        self.limits = wf.limits
        self.schemas = wf.schemas
        self.params = {**wf.params, **params}    # spec defaults, caller overrides
        self.preflight_checked = preflight_checked
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
        self._worktree_consolidate_lock = asyncio.Lock()

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
        try:
            # Pre-flight check before execution unless the caller already validated.
            if not self.preflight_checked:
                check_preflight(
                    self.wf.execution,
                    self.runner.default_cwd,
                    ignore_paths=[self.runner.journal_dir],
                )

            status, aborted_at, pending_input = "DONE", None, None
            try:
                i = 0
                while i < len(self.plan):
                    batch = []
                    while i < len(self.plan) and self.plan[i].kind != "supervisor":
                        batch.append(self.plan[i])
                        i += 1

                    if batch:
                        tasks = {}
                        async def run_step_with_deps(step: Step):
                            for dep in step.needs:
                                if dep in tasks:
                                    await tasks[dep]

                            if step.id in self._completed:
                                result = self._completed[step.id]
                                self.ctx["steps"][step.id] = result
                                log(f"RESUMED step {step.id} (journaled, 0 agents)")
                                return

                            if step.when is not None and not _eval_when(step.when, self.ctx):
                                log(f"SKIPPED  {step.id}: when=false (0 agents)")
                                result = {"skipped": True, "ok": True, "kind": step.kind}
                                self.ctx["steps"][step.id] = result
                                self._completed[step.id] = result
                                self._persist_steps()
                                self.runner._emit(
                                    step.id, "skipped", kind=step.kind, when=step.when)
                                return

                            # A skipped dependency satisfies `needs`; downstream steps
                            # only skip when their own `when` says so.
                            result = await self._exec_step(step, self.ctx, step.id)
                            self.ctx["steps"][step.id] = result
                            self._completed[step.id] = result
                            self._persist_steps()

                        for step in batch:
                            tasks[step.id] = asyncio.create_task(run_step_with_deps(step))

                        done, pending = await asyncio.wait(
                            tasks.values(),
                            return_when=asyncio.FIRST_EXCEPTION,
                        )
                        if pending:
                            for t in pending:
                                t.cancel()
                            await asyncio.gather(*pending, return_exceptions=True)

                        first_exc: BaseException | None = None
                        for t in done:
                            try:
                                t.result()
                            except BaseException as e:  # noqa: BLE001 - propagate after draining siblings
                                if first_exc is None or (
                                    isinstance(first_exc, _Pause) and not isinstance(e, _Pause)
                                ):
                                    first_exc = e
                        if first_exc is not None:
                            raise first_exc

                    if i < len(self.plan) and self.plan[i].kind == "supervisor":
                        step = self.plan[i]
                        if step.id in self._completed:
                            result = self._completed[step.id]
                            self.ctx["steps"][step.id] = result
                            self._apply_supervision(result.get("value") or {}, i, replay=True)
                        else:
                            result = await self._exec_supervisor(step, i)
                            self.ctx["steps"][step.id] = result
                            self._completed[step.id] = result
                            self._persist_steps()
                        i += 1
            except _Abort as a:
                status, aborted_at = "ABORTED", a.step_id
            except _Pause as p:
                status, pending_input = "PAUSED", p.request
            out = render(self.wf.output, self.ctx) if self.wf.output is not None else None
            if status == "DONE":
                self._validate_artifacts()
            bundle = {
                "status": status,
                "name": self.wf.name,
                "output": out,
                "steps": {sid: r.get("value") for sid, r in self.ctx["steps"].items()},
            }
            if aborted_at:
                bundle["aborted_at"] = aborted_at
            if pending_input is not None:
                bundle["pending_input"] = pending_input
            return bundle
        finally:
            teardown = getattr(self.runner, "teardown", None)
            if teardown is not None:
                await teardown()

    def _validate_artifacts(self) -> None:
        if not self.wf.artifacts:
            return
        root = Path(self.runner.default_cwd or os.getcwd())
        missing: list[str] = []
        for artifact in self.wf.artifacts:
            if isinstance(artifact, str):
                raw_path = artifact
                kind = "file"
            elif isinstance(artifact, dict):
                raw_path = artifact.get("path")
                kind = artifact.get("type", "file")
            else:
                continue
            rendered = render(raw_path, self.ctx)
            if not isinstance(rendered, str) or not rendered:
                missing.append(str(raw_path))
                continue
            path = Path(rendered)
            if not path.is_absolute():
                path = root / path
            exists = path.is_dir() if kind == "dir" else path.is_file()
            if not exists:
                missing.append(str(path))
        if missing:
            raise WorkflowError("required workflow artifact(s) missing: " + ", ".join(missing))

    # --- schema / agent helpers ------------------------------------------
    def _schema(self, schema: str | dict[str, Any] | None) -> dict[str, Any] | None:
        if schema is None or isinstance(schema, dict):
            return schema
        resolved = self.schemas.get(schema)
        if resolved is None:
            raise WorkflowError(f"unknown schema name {schema!r}")
        return resolved

    async def _agent_call(
        self,
        a: AgentSpec,
        ctx: dict[str, Any],
        label: str,
        *,
        cwd: Path | None = None,
    ) -> AgentResult:
        self.calls += 1
        if self.calls > self.limits.max_total_agent_calls:
            raise WorkflowLimitError(
                f"workflow exceeded max_total_agent_calls={self.limits.max_total_agent_calls}")
        kwargs = {
            "label": label,
            "schema": self._schema(a.schema),
            "prefer": a.prefer,
            "model": a.model,
            "models": a.models,
            "role": a.role,
            "sandbox": a.sandbox,
            "tools": a.tools,
            "timeout_s": a.timeout_s,
            "heartbeat_interval_s": a.heartbeat_interval_s,
        }
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        return await self.runner.agent(render(a.prompt, ctx), **kwargs)

    def _write_guard_needed(self, a: AgentSpec) -> bool:
        return a.sandbox != "read-only" or "write" in set(a.tools or [])

    def _git_dirty_paths(self, cwd: Path | None = None) -> set[str]:
        root = cwd or Path(self.runner.default_cwd or os.getcwd())
        if not root.exists():
            return set()
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return set()
        dirty: set[str] = set()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            path = line[3:] if len(line) > 3 else line
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path and not path.startswith(".iworkflow/"):
                dirty.add(path)
        return dirty

    def _git_root(self, cwd: Path | None = None) -> Path | None:
        root = cwd or Path(self.runner.default_cwd or os.getcwd())
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return Path(result.stdout.strip())

    def _allowed_write_paths(
        self,
        a: AgentSpec,
        ctx: dict[str, Any],
        cwd: Path | None = None,
        base_cwd: Path | None = None,
    ) -> list[str]:
        workflow_root = cwd or Path(self.runner.default_cwd or os.getcwd())
        git_root = self._git_root(cwd) or workflow_root
        base_git_root = self._git_root(base_cwd) if base_cwd is not None else None
        allowed: list[str] = []
        for raw in a.write_paths:
            rendered = render(raw, ctx)
            if not isinstance(rendered, str) or not rendered:
                continue
            path = Path(rendered)
            if not path.is_absolute():
                path = workflow_root / path
                roots = [git_root]
            else:
                roots = [git_root]
                if base_git_root is not None:
                    roots.append(base_git_root)
            normalized = path.as_posix()
            for root in roots:
                try:
                    normalized = path.resolve().relative_to(root.resolve()).as_posix()
                    break
                except ValueError:
                    continue
            allowed.append(normalized.rstrip("/"))
        return allowed

    @staticmethod
    def _is_allowed_write(path: str, allowed: list[str]) -> bool:
        normalized = path.rstrip("/")
        for allow in allowed:
            if normalized == allow or normalized.startswith(allow.rstrip("/") + "/"):
                return True
        return False

    def _validate_write_paths(
        self,
        a: AgentSpec,
        ctx: dict[str, Any],
        label: str,
        before_dirty: set[str],
        *,
        cwd: Path | None = None,
        base_cwd: Path | None = None,
    ) -> None:
        after_dirty = self._git_dirty_paths(cwd)
        new_dirty = sorted(after_dirty - before_dirty)
        if not new_dirty:
            return
        allowed = self._allowed_write_paths(a, ctx, cwd, base_cwd)
        disallowed = [p for p in new_dirty if not self._is_allowed_write(p, allowed)]
        if disallowed:
            allowed_text = ", ".join(allowed) if allowed else "<none>"
            raise WorkflowError(
                f"agent step {label!r} wrote outside allowed paths: "
                f"{', '.join(disallowed)} (allowed: {allowed_text})"
            )

    def _base_cwd(self) -> Path:
        return Path(self.runner.default_cwd or os.getcwd())

    def _require_worktree_git_repo(self) -> Path:
        base_cwd = self._base_cwd()
        if not _is_git_repo(base_cwd):
            raise WorkflowError(
                "worktree isolation required for write-capable agents in "
                "parallel/pipeline steps, but working directory is not a git "
                f"repository: {base_cwd}"
            )
        return base_cwd

    def _raise_required_failure(self, a: AgentSpec, res: AgentResult, label: str) -> None:
        if res.ok or not a.required:
            return
        attempts = ", ".join(
            f"{x.provider}:{x.outcome}{f' ({x.detail})' if x.detail else ''}"
            for x in res.attempts
        )
        timeout = f", timeout_s={a.timeout_s}" if a.timeout_s is not None else ""
        raise WorkflowError(
            f"agent step {label!r} exhausted without a result ({attempts}{timeout})"
        )

    async def _agent_call_with_worktree(
        self,
        a: AgentSpec,
        ctx: dict[str, Any],
        label: str,
        *,
        step_id: str,
        agent_id: str,
        base_cwd: Path,
    ) -> AgentResult:
        wt_path = _worktree_path(self.runner.run_id, step_id, agent_id)
        branch = (
            f"iwf/{_worktree_component(self.runner.run_id)}-"
            f"{_worktree_component(step_id)}-{_worktree_component(agent_id)}"
        )
        await _create_worktree(base_cwd, wt_path, branch)
        try:
            before_dirty = await asyncio.to_thread(self._git_dirty_paths, wt_path)
            res = await self._agent_call(a, ctx, label, cwd=wt_path)
            if res.ok:
                await asyncio.to_thread(
                    self._validate_write_paths,
                    a,
                    ctx,
                    label,
                    before_dirty,
                    cwd=wt_path,
                    base_cwd=base_cwd,
                )
                async with self._worktree_consolidate_lock:
                    await _consolidate_worktree(base_cwd, wt_path)
            else:
                self._raise_required_failure(a, res, label)
            return res
        finally:
            await _remove_worktree(base_cwd, wt_path)

    @staticmethod
    def _result(res: AgentResult, **extra: Any) -> dict[str, Any]:
        return {"value": res.value, "provider": res.provider, "ok": res.ok, "timeout": res.timeout, "last_heartbeat": res.last_heartbeat, **extra}

    # --- dispatch ---------------------------------------------------------
    async def _exec_step(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        if step.kind == "agent":
            return await self._exec_agent(step, ctx, label)
        if step.kind == "command":
            return await self._exec_command(step, ctx, label)
        if step.kind == "checkpoint":
            return await self._exec_checkpoint(step, ctx, label)
        if step.kind == "parallel":
            return await self._exec_parallel(step, ctx, label)
        if step.kind == "pipeline":
            return await self._exec_pipeline(step, ctx, label)
        if step.kind == "loop":
            return await self._exec_loop(step, ctx, label)
        # supervisor is top-level only (run() handles it); anything else is a bug.
        raise WorkflowError(f"cannot execute step kind {step.kind!r} here")

    def _checkpoint_path(self, raw: Any, ctx: dict[str, Any]) -> Path | None:
        rendered = render(raw, ctx)
        if not isinstance(rendered, str) or not rendered.strip():
            return None
        path = Path(rendered)
        if not path.is_absolute():
            path = Path(self.runner.default_cwd or os.getcwd()) / path
        return path

    def _checkpoint_request(
        self,
        step: Step,
        ctx: dict[str, Any],
        *,
        validation_error: str | None = None,
    ) -> dict[str, Any]:
        cp = step.checkpoint or {}
        schema_ref = cp.get("schema")
        request: dict[str, Any] = {
            "step_id": step.id,
            "mode": cp.get("mode", "approval"),
            "title": render(cp.get("title"), ctx),
            "prompt": render(cp.get("prompt"), ctx),
            "artifact": None,
            "schema": self._schema(schema_ref),
            "output": None,
        }
        if isinstance(schema_ref, str):
            request["schema_name"] = schema_ref
        artifact_path = self._checkpoint_path(cp.get("artifact"), ctx) if cp.get("artifact") else None
        if artifact_path is not None:
            request["artifact"] = str(artifact_path)
        output_path = self._checkpoint_path(cp.get("output"), ctx)
        if output_path is not None:
            request["output"] = str(output_path)
        if validation_error:
            request["validation_error"] = validation_error
        return request

    def _load_checkpoint_output(self, path: Path) -> tuple[bool, Any, str | None]:
        try:
            return True, json.loads(path.read_text(encoding="utf-8")), None
        except OSError as e:
            return False, None, f"cannot read output {path}: {e}"
        except json.JSONDecodeError as e:
            return False, None, f"checkpoint output {path} is not valid JSON: {e}"

    def _validate_checkpoint_resolution(
        self,
        step: Step,
        value: Any,
    ) -> tuple[bool, str | None]:
        cp = step.checkpoint or {}
        schema = self._schema(cp.get("schema"))
        if schema is not None:
            ok, why = validate_json_schema(value, schema)
            if not ok:
                return False, why
        if cp.get("mode") == "confirm" and not self._is_explicit_confirmation(value):
            return False, "confirm checkpoint requires explicit affirmative resolution"
        return True, None

    @staticmethod
    def _is_explicit_confirmation(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {
                "approve", "approved", "go", "send", "yes", "y", "si", "sí",
                "envia", "envía", "envialo", "envíalo",
            }
        if not isinstance(value, dict):
            return False
        for key in ("approved", "approve", "confirmed", "confirm", "go", "send"):
            if value.get(key) is True:
                return True
        decision = value.get("decision", value.get("action", value.get("status")))
        if isinstance(decision, str):
            return decision.strip().lower() in {
                "approve", "approved", "go", "send", "yes", "y", "si", "sí",
                "envia", "envía", "envialo", "envíalo",
            }
        return False

    def _write_checkpoint_output(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _pause_checkpoint(
        self,
        step: Step,
        ctx: dict[str, Any],
        *,
        validation_error: str | None = None,
    ) -> None:
        request = self._checkpoint_request(step, ctx, validation_error=validation_error)
        self.runner._emit(step.id, "checkpoint_pending", **request)
        log(f"PAUSED   {step.id}: waiting for human checkpoint resolution")
        raise _Pause(step.id, request)

    async def _exec_checkpoint(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        cp = step.checkpoint
        assert cp is not None
        output_path = self._checkpoint_path(cp.get("output"), ctx)

        if output_path is not None and output_path.exists():
            ok, value, why = self._load_checkpoint_output(output_path)
            if not ok:
                self._pause_checkpoint(step, ctx, validation_error=why)
            valid, validation_error = self._validate_checkpoint_resolution(step, value)
            if not valid:
                self._pause_checkpoint(step, ctx, validation_error=validation_error)
            self.runner._emit(
                label,
                "done",
                provider="human",
                kind="checkpoint",
                mode=cp.get("mode", "approval"),
                output=str(output_path),
            )
            return {
                "kind": "checkpoint",
                "value": value,
                "ok": True,
                "mode": cp.get("mode", "approval"),
                "output": str(output_path),
            }

        resolver = getattr(self.runner, "checkpoint_resolver", None)
        if resolver is None:
            self._pause_checkpoint(step, ctx)

        request = self._checkpoint_request(step, ctx)
        value = resolver(request)
        if asyncio.iscoroutine(value):
            value = await value
        valid, validation_error = self._validate_checkpoint_resolution(step, value)
        if not valid:
            self._pause_checkpoint(step, ctx, validation_error=validation_error)
        if output_path is not None:
            self._write_checkpoint_output(output_path, value)
        self.runner._emit(
            label,
            "done",
            provider="human",
            kind="checkpoint",
            mode=cp.get("mode", "approval"),
            output=str(output_path) if output_path is not None else None,
        )
        return {
            "kind": "checkpoint",
            "value": value,
            "ok": True,
            "mode": cp.get("mode", "approval"),
            "output": str(output_path) if output_path is not None else None,
        }

    async def _exec_command(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        import time

        # 1. Resolve and render command
        raw_cmd = step.command
        if isinstance(raw_cmd, str):
            rendered_cmd = render(raw_cmd, ctx)
            argv = [rendered_cmd]
            shell = True
        else:
            argv = [render(arg, ctx) for arg in raw_cmd]
            shell = False

        # 2. Resolve CWD and env
        wf_cwd = self.runner.default_cwd or os.getcwd()
        step_cwd = render(step.cwd, ctx) if step.cwd else None
        exec_cwd = os.path.abspath(step_cwd) if step_cwd else wf_cwd
        if not os.path.isabs(exec_cwd) and step_cwd:
            exec_cwd = os.path.abspath(os.path.join(wf_cwd, step_cwd))

        exec_env = {**os.environ}
        if step.env:
            for k, v in step.env.items():
                exec_env[k] = render(v, ctx)

        # 3. Log and run subprocess
        log(f"COMMAND  {label} → Running: {argv} (cwd={exec_cwd})")
        t_start = time.time()
        timeout = step.timeout_s or 60.0
        self.runner._emit(label, "dispatch", provider="local", kind="command", cwd=exec_cwd)

        if shell:
            proc = await asyncio.create_subprocess_shell(
                argv[0],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=exec_cwd,
                env=exec_env,
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=exec_cwd,
                env=exec_env,
                start_new_session=True,
            )

        pgid = proc.pid
        self.runner.register_pgid(pgid)

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []

        async def read_stream(stream: asyncio.StreamReader | None, name: str, parts: list[bytes]) -> None:
            if stream is None:
                return
            buffer = bytearray()

            def emit(data: bytes) -> None:
                if not data:
                    return
                parts.append(data)
                text = data.decode(errors="replace")
                if text:
                    self.runner._emit(label, "output", provider="local", stream=name, text=text)

            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffer[: newline + 1])
                    del buffer[: newline + 1]
                    emit(line)
                if len(buffer) > 1_000_000:
                    emit(bytes(buffer))
                    buffer.clear()

            if buffer:
                emit(bytes(buffer))

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    read_stream(proc.stdout, "stdout", stdout_parts),
                    read_stream(proc.stderr, "stderr", stderr_parts),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            self.runner._emit(label, "timeout", provider="local", timeout_s=timeout)
        finally:
            try:
                import signal
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            self.runner.unregister_pgid(pgid)

        duration = time.time() - t_start
        exit_code = 124 if timed_out else proc.returncode
        stdout_str = b"".join(stdout_parts).decode(errors="replace")
        stderr_str = b"".join(stderr_parts).decode(errors="replace")

        self.runner._emit(
            label,
            "done",
            provider="local",
            exit_code=exit_code,
            ms=round(duration * 1000),
            timeout=timed_out,
        )
        log(f"DONE     {label} ← exit={exit_code} ({duration:.2f}s)")
        
        value = {
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "timeout": timed_out,
            "duration_s": duration,
        }

        # 4. Handle gate checking if present
        if step.gate:
            field_name = step.gate.get("field", "exit_code")
            val = value.get(field_name)
            abort_on = step.gate.get("abort_on", [1])
            if not isinstance(abort_on, (list, tuple, set)):
                abort_set = {abort_on}
            else:
                abort_set = set(abort_on)
            if val in abort_set or (field_name == "exit_code" and "non-zero" in abort_set and val != 0):
                raise _Abort(step.id)

        return {
            "value": value,
            "ok": exit_code == 0,
            "timeout": timed_out,
        }

    async def _exec_agent(self, step: Step, ctx: dict[str, Any], label: str) -> dict[str, Any]:
        a = step.agent
        assert a is not None
        wg = self._write_guard_needed(a)
        before_dirty = await asyncio.to_thread(self._git_dirty_paths) if wg else set()
        res = await self._agent_call(a, ctx, label)
        if res.ok and wg:
            await asyncio.to_thread(self._validate_write_paths, a, ctx, label, before_dirty)
        out = self._result(res, kind="agent")
        self._raise_required_failure(a, res, label)
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
        need_worktree = any(self._write_guard_needed(a) for a in step.agents)
        base_cwd = (
            await asyncio.to_thread(self._require_worktree_git_repo)
            if need_worktree else None
        )

        def thunk(a: AgentSpec):
            async def run() -> AgentResult:
                agent_label = f"{label}:{a.id}"
                if self._write_guard_needed(a):
                    assert base_cwd is not None
                    return await self._agent_call_with_worktree(
                        a,
                        ctx,
                        agent_label,
                        step_id=step.id,
                        agent_id=a.id,
                        base_cwd=base_cwd,
                    )
                return await self._agent_call(a, ctx, agent_label)
            return run

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

        need_worktree = any(self._write_guard_needed(a) for a in step.stages)
        base_cwd = (
            await asyncio.to_thread(self._require_worktree_git_repo)
            if need_worktree else None
        )

        def make_stage(a: AgentSpec):
            async def run(prev: Any, item: Any, idx: int) -> Any:
                sctx = {**ctx, "item": item, "prev": prev, "index": idx}
                agent_label = f"{label}:{a.id}#{idx}"
                if self._write_guard_needed(a):
                    assert base_cwd is not None
                    res = await self._agent_call_with_worktree(
                        a,
                        sctx,
                        agent_label,
                        step_id=step.id,
                        agent_id=f"{a.id}-{idx}",
                        base_cwd=base_cwd,
                    )
                else:
                    res = await self._agent_call(a, sctx, agent_label)
                return self._result(res)
            return run

        stages = [make_stage(a) for a in step.stages]
        if base_cwd is None:
            out = await self.runner.pipeline(items, *stages)
        else:
            async def run_item(item: Any, idx: int) -> Any:
                val = item
                for stage in stages:
                    val = await stage(val, item, idx)
                return val
            out = list(await asyncio.gather(
                *(run_item(it, i) for i, it in enumerate(items))))
        return {"kind": "pipeline", "value": out, "ok": all(
            isinstance(x, dict) and x.get("ok") is True for x in out)}

    # --- the supervisor (adaptive re-planning) ---------------------------
    def _supervisor_state(self, step: Step) -> dict[str, Any]:
        """The accumulated run state handed to the coordinator. `watch` (if set)
        filters which prior steps' values are exposed; `remaining` lists the steps
        still ahead so the coordinator can reason about what's left to do."""
        watch = step.watch
        steps_info = {sid: r for sid, r in self.ctx["steps"].items() if watch is None or sid in watch}
        steps_state = {sid: r.get("value") for sid, r in self.ctx["steps"].items()
                       if watch is None or sid in watch}
        done = set(self.ctx["steps"])
        remaining = [s.id for s in self.plan
                     if s.id not in done and s.kind != "supervisor"]
        return {"steps": steps_state, "steps_info": steps_info, "remaining": remaining, "params": dict(self.params)}

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
                          models=p.get("models"), role=p.get("role"),
                          timeout_s=p.get("timeout_s"),
                          heartbeat_interval_s=p.get("heartbeat_interval_s"),
                          required=bool(p.get("required", True))),
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
                          models=p.get("models"), timeout_s=p.get("timeout_s"),
                          heartbeat_interval_s=p.get("heartbeat_interval_s"),
                          required=bool(p.get("required", True))),
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

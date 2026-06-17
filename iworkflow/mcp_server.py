"""Optional MCP face — let any MCP client (Codex, agy, Claude) DRIVE iworkflow.

This is the piece that closes the original goal: Codex/Gemini don't have a native
"Workflow tool", so we expose one over MCP. The tool LOGIC (`run_workflow`, `ping`)
has NO mcp dependency and is unit-testable with FakeProviders; `main()` is the thin
FastMCP stdio wrapper (needs the optional `[mcp]` extra).

Serve:  python -m iworkflow.mcp_server
Register with Codex (per-invocation, no global config pollution):
  codex exec -c 'mcp_servers.iworkflow.command="python"' \
             -c 'mcp_servers.iworkflow.args=["-m","iworkflow.mcp_server"]' \
             "Call the iworkflow workflow tool with goal=..."

Long-running workflows (typical MCP tool timeout ~30s):
  1. iworkflow_workflow_start(goal=...)  -> {run_id, status: "started"}
  2. iworkflow_workflow_stream(run_id, after=0, block_s=5)  # incremental events
     or iworkflow_workflow_poll(run_id)  # snapshot poll
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .catalog_loaders import load_project_catalog
from .ledger import sha
from .providers import ClaudeInteractiveProvider, CodexProvider, CursorProvider, GeminiProvider
from .recipes import get_recipe, list_recipes
from .scheduler import Runner
from .toolsets import ToolCatalog
from .workflow import run_spec, Limits

# In-process job registry for start/poll (MCP clients with short tool timeouts).
_jobs: dict[str, asyncio.Task] = {}
DEFAULT_COOLDOWN_S = 300.0

SYNC_WORKFLOW_DOC = """\
[DEPRECATED for long runs] Blocking workflow — returns only when finished.

Most MCP clients time out around 30s. For anything non-trivial prefer:
  1. iworkflow_workflow_start(...) -> {run_id}
  2. iworkflow_workflow_stream(run_id, after=<cursor>, block_s=5)
     or iworkflow_workflow_poll(run_id)

Pass exactly one driver: spec, workflow+params, or goal."""


def ping() -> dict[str, str]:
    """Cheap liveness tool — proves an MCP client can reach iworkflow."""
    return {"ok": "pong from iworkflow", "engine": "subscription-only multi-agent"}


def _resolve_run_id(run_id: str, goal: str | None, params: dict[str, Any] | None) -> str:
    """Isolate default shared `mcp` runs by goal/params fingerprint."""
    if run_id != "mcp":
        return run_id
    key = goal if goal is not None else json.dumps(params or {}, sort_keys=True, default=str)
    return f"mcp-{sha(key)}"


def _resolve_catalog(catalog_root: str | None, cwd: str | None) -> ToolCatalog | None:
    """Load a project tool catalog when a root directory is provided."""
    root = catalog_root or cwd
    if root is None:
        return None
    return load_project_catalog(root)


def _default_runner(
    run_id: str,
    *,
    cwd: str | None = None,
    timeout_s: float = 180,
    caps: dict[str, int] | None = None,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    learn: bool = True,
    catalog: ToolCatalog | None = None,
    journal_dir: str = ".iworkflow",
) -> Runner:
    caps = caps or {"codex": 2, "gemini": 2, "claude": 1, "cursor": 2, "cursor_flash": 2}
    providers = {
        "codex": CodexProvider("codex", timeout_s=timeout_s),
        "gemini": GeminiProvider("gemini", timeout_s=timeout_s),
        "claude": ClaudeInteractiveProvider("claude", timeout_s=timeout_s),
        "cursor": CursorProvider("cursor", model="composer-2.5", timeout_s=timeout_s),
        "cursor_flash": CursorProvider(
            "cursor_flash", model="composer-2.5-flash", timeout_s=timeout_s,
        ),
    }
    return Runner(
        run_id,
        providers,
        caps,
        journal_dir=journal_dir,
        cooldown_s=cooldown_s,
        learn=learn,
        catalog=catalog,
        default_cwd=cwd,
    )


def _maybe_degrade_fan_synthesize(result: dict[str, Any]) -> dict[str, Any]:
    """If synth exhausted, return the best surviving proposal instead of None."""
    if result.get("output") not in (None, ""):
        return result
    if result.get("name") != "fan_synthesize":
        return result
    fan = result.get("steps", {}).get("fan")
    proposals: list[Any] = []
    if isinstance(fan, list):
        proposals = [p for p in fan if p]
    elif fan:
        proposals = [fan]
    if not proposals:
        return result
    best = max(proposals, key=lambda p: len(str(p)) if p is not None else 0)
    return {
        **result,
        "output": best,
        "degraded": True,
        "degraded_reason": "synth exhausted; using best proposal",
    }


def _events_path(run_id: str, journal_dir: str = ".iworkflow") -> Path:
    return Path(journal_dir) / "runs" / run_id / "events.jsonl"


def _iter_event_lines(path: Path) -> Iterator[str]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            yield line


def _parse_event_line(line: str) -> dict | None:
    if not line.strip():
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) else None


def _read_events_since(
    run_id: str,
    journal_dir: str = ".iworkflow",
    *,
    after: int = 0,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """Return parsed events from events.jsonl starting at line offset `after`."""
    path = _events_path(run_id, journal_dir)
    events: list[dict] = []
    line_no = 0
    for line in _iter_event_lines(path):
        if line_no < after:
            line_no += 1
            continue
        if len(events) >= limit:
            break
        rec = _parse_event_line(line)
        line_no += 1
        if rec is not None:
            events.append(rec)
    return events, line_no


def _tail_events(run_id: str, journal_dir: str = ".iworkflow", limit: int = 20) -> list[dict]:
    path = _events_path(run_id, journal_dir)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[dict] = []
    for line in lines[-limit:]:
        rec = _parse_event_line(line)
        if rec is not None:
            events.append(rec)
    return events


def _workflow_status(
    run_id: str, journal_dir: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Infer run status from in-process task, else on-disk events."""
    task = _jobs.get(run_id)
    if task is not None:
        if not task.done():
            return "running", None, None
        try:
            return "done", task.result(), None
        except Exception as e:  # noqa: BLE001
            return "error", None, str(e)

    events = _tail_events(run_id, journal_dir, limit=5)
    steps_path = Path(journal_dir) / "runs" / run_id / "wf-steps.json"
    if steps_path.exists() and events:
        last = events[-1]
        if last.get("event") in {"done", "exhausted"}:
            return "unknown_done", None, (
                "in-process task gone; re-run workflow_start or inspect ledger"
            )
    return "unknown", None, None


async def run_workflow(goal: str | None = None, *, workflow: str | None = None,
                       params: dict[str, Any] | None = None,
                       spec: dict[str, Any] | None = None,
                       run_id: str = "mcp", recipe_dir: str | None = None,
                       runner: Runner | None = None,
                       cwd: str | None = None,
                       timeout_s: float = 180,
                       caps: dict[str, int] | None = None,
                       catalog_root: str | None = None,
                       journal_dir: str = ".iworkflow",
                       allow_tools: bool = False) -> dict[str, Any]:
    """Run a subscription-only multi-agent workflow. Three ways to drive it:

    - `spec=`     : a declarative workflow spec (define your own — DYNAMIC door).
    - `workflow=` : a named recipe (built-in or host-registered) + `params`.
    - `goal=`     : sugar for the `fan_synthesize` recipe over a single question.

    `runner` is injectable so tests pass a FakeProvider-backed Runner (no quota).
    """
    rid = _resolve_run_id(run_id, goal, params)
    r = runner or _default_runner(
        rid, cwd=cwd, timeout_s=timeout_s, caps=caps,
        catalog=_resolve_catalog(catalog_root, cwd),
        journal_dir=journal_dir,
    )
    limits = Limits(allow_tools=allow_tools)
    if spec is not None:
        result = await run_spec(r, spec, params, limits=limits)
    elif workflow is not None:
        result = await run_spec(r, get_recipe(workflow, recipe_dir), params, limits=limits)
    elif goal is not None:
        result = await run_spec(r, get_recipe("fan_synthesize", recipe_dir), {"goal": goal}, limits=limits)
    result = _maybe_degrade_fan_synthesize(result)
    result["run_id"] = rid
    return result


async def workflow_start(goal: str | None = None, *, workflow: str | None = None,
                         params: dict[str, Any] | None = None,
                         spec: dict[str, Any] | None = None,
                         run_id: str = "mcp", recipe_dir: str | None = None,
                         runner: Runner | None = None,
                         cwd: str | None = None,
                         timeout_s: float = 180,
                         caps: dict[str, int] | None = None,
                         catalog_root: str | None = None,
                         journal_dir: str = ".iworkflow",
                         allow_tools: bool = False) -> dict[str, Any]:
    """Start a workflow in the background; poll/stream with run_id."""
    rid = _resolve_run_id(run_id, goal, params)
    existing = _jobs.get(rid)
    if existing is not None and not existing.done():
        return {"run_id": rid, "status": "running"}

    async def _work() -> dict[str, Any]:
        return await run_workflow(
            goal, workflow=workflow, params=params, spec=spec, run_id=rid,
            recipe_dir=recipe_dir, runner=runner, cwd=cwd, timeout_s=timeout_s,
            caps=caps, catalog_root=catalog_root, journal_dir=journal_dir,
            allow_tools=allow_tools,
        )

    task = asyncio.create_task(_work())
    _jobs[rid] = task

    def _cleanup(t: asyncio.Task) -> None:
        if _jobs.get(rid) is t:
            del _jobs[rid]

    task.add_done_callback(_cleanup)
    return {"run_id": rid, "status": "started"}


async def workflow_poll(run_id: str, journal_dir: str = ".iworkflow") -> dict[str, Any]:
    """Poll a background workflow started via `workflow_start`."""
    status, result, hint = _workflow_status(run_id, journal_dir)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "events": _tail_events(run_id, journal_dir),
    }
    if result is not None:
        payload["result"] = result
    if hint:
        payload["hint"] = hint
    if status == "error":
        payload["error"] = hint
    return payload


async def workflow_stream(
    run_id: str,
    *,
    journal_dir: str = ".iworkflow",
    after: int = 0,
    block_s: float = 0.0,
    limit: int = 50,
) -> dict[str, Any]:
    """Incremental SSE-like stream of events.jsonl for a background workflow.

    Call repeatedly with the returned `next_after` cursor. Set `block_s>0` to wait
    for new events (long-poll) instead of returning immediately when caught up.
    """
    deadline = time.time() + max(block_s, 0.0)
    cursor = after
    events: list[dict] = []

    while True:
        batch, cursor = _read_events_since(
            run_id, journal_dir, after=cursor, limit=limit - len(events),
        )
        events.extend(batch)
        status, result, hint = _workflow_status(run_id, journal_dir)
        terminal = status in {"done", "error", "unknown_done"}
        if events or terminal or block_s <= 0 or time.time() >= deadline:
            break
        await asyncio.sleep(0.25)

    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "events": events,
        "next_after": cursor,
        "stream": "events.jsonl",
    }
    if result is not None:
        payload["result"] = result
    if hint:
        payload["hint"] = hint
    if status == "error":
        payload["error"] = hint
    return payload



def check_sessions(
    providers: list[str] | None = None,
    timeout_s: float = 12.0,
) -> dict[str, Any]:
    """Probe subscription CLI login state for workflow workers."""
    from .sessions import probe_sessions

    return probe_sessions(providers, timeout_s=timeout_s)

def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("iworkflow")

    @server.tool()
    def iworkflow_ping() -> dict[str, str]:
        """Liveness check for the iworkflow engine."""
        return ping()

    @server.tool()
    def iworkflow_list_workflows(recipe_dir: str | None = None) -> list[dict[str, Any]]:
        """List workflow recipes (built-in + host-registered under recipe_dir).

        `recipe_dir` defaults to built-ins only; set to `.iworkflow/recipes` or a
        custom path to include host-project recipes."""
        return list_recipes(recipe_dir)

    @server.tool()
    async def iworkflow_workflow_start(
        goal: str | None = None,
        workflow: str | None = None,
        params: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
        run_id: str = "mcp",
        recipe_dir: str | None = None,
        cwd: str | None = None,
        timeout_s: float = 180,
        caps: dict[str, int] | None = None,
        catalog_root: str | None = None,
        journal_dir: str = ".iworkflow",
        allow_tools: bool = False,
    ) -> dict[str, Any]:
        """Start a long-running workflow without blocking the MCP client.

        Prefer this over `iworkflow_workflow` when the run may exceed ~30s.
        Follow with `iworkflow_workflow_stream` (incremental) or
        `iworkflow_workflow_poll` (snapshot).

        `caps` sets per-provider concurrency, e.g. {"codex": 2, "gemini": 2}.
        `catalog_root` loads MCP/skills/commands from a repo (same as CLI catalog).
        `recipe_dir` adds host recipes from `.iworkflow/recipes` or a custom path."""
        return await workflow_start(
            goal, workflow=workflow, params=params, spec=spec, run_id=run_id,
            recipe_dir=recipe_dir, cwd=cwd, timeout_s=timeout_s, caps=caps,
            catalog_root=catalog_root, journal_dir=journal_dir,
            allow_tools=allow_tools,
        )

    @server.tool()
    async def iworkflow_workflow_poll(
        run_id: str,
        journal_dir: str = ".iworkflow",
    ) -> dict[str, Any]:
        """Poll a workflow started with iworkflow_workflow_start."""
        return await workflow_poll(run_id, journal_dir=journal_dir)

    @server.tool()
    async def iworkflow_workflow_stream(
        run_id: str,
        after: int = 0,
        block_s: float = 5.0,
        limit: int = 50,
        journal_dir: str = ".iworkflow",
    ) -> dict[str, Any]:
        """Stream workflow progress from events.jsonl (SSE-like incremental poll).

        Returns new events since `after` (line offset). Use the returned
        `next_after` on the next call. Set `block_s` to long-poll for new events
        (default 5s). When `status` is `done` or `error`, `result` is included."""
        return await workflow_stream(
            run_id, journal_dir=journal_dir, after=after,
            block_s=block_s, limit=limit,
        )

    @server.tool()
    async def iworkflow_workflow(
        goal: str | None = None,
        workflow: str | None = None,
        params: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
        run_id: str = "mcp",
        recipe_dir: str | None = None,
        cwd: str | None = None,
        timeout_s: float = 180,
        caps: dict[str, int] | None = None,
        catalog_root: str | None = None,
        journal_dir: str = ".iworkflow",
        allow_tools: bool = False,
    ) -> dict[str, Any]:
        return await run_workflow(
            goal, workflow=workflow, params=params, spec=spec, run_id=run_id,
            recipe_dir=recipe_dir, cwd=cwd, timeout_s=timeout_s, caps=caps,
            catalog_root=catalog_root, journal_dir=journal_dir,
            allow_tools=allow_tools,
        )

    # Attach deprecation doc to the sync tool (FastMCP uses __doc__ for schema).
    iworkflow_workflow.__doc__ = SYNC_WORKFLOW_DOC

    @server.tool()
    def iworkflow_sessions(
        providers: list[str] | None = None,
        timeout_s: float = 12.0,
    ) -> dict[str, Any]:
        """Check which subscription CLIs are logged in (codex, claude, gemini, cursor).

        Run before dispatching work to avoid silent failures. Optional `providers`
        subset: codex, claude, gemini, cursor, cursor_flash."""
        return check_sessions(providers, timeout_s=timeout_s)

    server.run()


if __name__ == "__main__":
    main()

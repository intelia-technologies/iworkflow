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
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .ledger import sha
from .providers import ClaudeInteractiveProvider, CodexProvider, GeminiProvider
from .recipes import get_recipe, list_recipes
from .scheduler import Runner
from .toolsets import ToolCatalog
from .workflow import run_spec

# In-process job registry for start/poll (MCP clients with short tool timeouts).
_jobs: dict[str, asyncio.Task] = {}
DEFAULT_COOLDOWN_S = 300.0


def ping() -> dict[str, str]:
    """Cheap liveness tool — proves an MCP client can reach iworkflow."""
    return {"ok": "pong from iworkflow", "engine": "subscription-only multi-agent"}


def _resolve_run_id(run_id: str, goal: str | None, params: dict[str, Any] | None) -> str:
    """Isolate default shared `mcp` runs by goal/params fingerprint."""
    if run_id != "mcp":
        return run_id
    key = goal if goal is not None else json.dumps(params or {}, sort_keys=True, default=str)
    return f"mcp-{sha(key)}"


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
    caps = caps or {"codex": 2, "gemini": 2, "claude": 1}
    providers = {
        "codex": CodexProvider("codex", timeout_s=timeout_s),
        "gemini": GeminiProvider("gemini", timeout_s=timeout_s),
        "claude": ClaudeInteractiveProvider("claude", timeout_s=timeout_s),
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


def _tail_events(run_id: str, journal_dir: str = ".iworkflow", limit: int = 20) -> list[dict]:
    path = Path(journal_dir) / "runs" / run_id / "events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[dict] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


async def run_workflow(goal: str | None = None, *, workflow: str | None = None,
                       params: dict[str, Any] | None = None,
                       spec: dict[str, Any] | None = None,
                       run_id: str = "mcp", recipe_dir: str | None = None,
                       runner: Runner | None = None,
                       cwd: str | None = None,
                       timeout_s: float = 180,
                       caps: dict[str, int] | None = None,
                       catalog: ToolCatalog | None = None,
                       journal_dir: str = ".iworkflow") -> dict[str, Any]:
    """Run a subscription-only multi-agent workflow. Three ways to drive it:

    - `spec=`     : a declarative workflow spec (define your own — DYNAMIC door).
    - `workflow=` : a named recipe (built-in or host-registered) + `params`.
    - `goal=`     : sugar for the `fan_synthesize` recipe over a single question.

    `runner` is injectable so tests pass a FakeProvider-backed Runner (no quota).
    """
    rid = _resolve_run_id(run_id, goal, params)
    r = runner or _default_runner(
        rid, cwd=cwd, timeout_s=timeout_s, caps=caps, catalog=catalog,
        journal_dir=journal_dir,
    )
    if spec is not None:
        result = await run_spec(r, spec, params)
    elif workflow is not None:
        result = await run_spec(r, get_recipe(workflow, recipe_dir), params)
    elif goal is not None:
        result = await run_spec(r, get_recipe("fan_synthesize", recipe_dir), {"goal": goal})
    else:
        raise ValueError("provide one of: spec, workflow, or goal")
    result = _maybe_degrade_fan_synthesize(result)
    result["run_id"] = rid
    return result


async def workflow_start(goal: str | None = None, *, workflow: str | None = None,
                         params: dict[str, Any] | None = None,
                         spec: dict[str, Any] | None = None,
                         run_id: str = "mcp", recipe_dir: str | None = None,
                         cwd: str | None = None,
                         timeout_s: float = 180,
                         caps: dict[str, int] | None = None,
                         journal_dir: str = ".iworkflow") -> dict[str, Any]:
    """Start a workflow in the background; poll with `workflow_poll(run_id)`."""
    rid = _resolve_run_id(run_id, goal, params)
    existing = _jobs.get(rid)
    if existing is not None and not existing.done():
        return {"run_id": rid, "status": "running"}

    async def _work() -> dict[str, Any]:
        return await run_workflow(
            goal, workflow=workflow, params=params, spec=spec, run_id=rid,
            recipe_dir=recipe_dir, cwd=cwd, timeout_s=timeout_s, caps=caps,
            journal_dir=journal_dir,
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
    task = _jobs.get(run_id)
    if task is not None:
        if not task.done():
            return {
                "run_id": run_id,
                "status": "running",
                "events": _tail_events(run_id, journal_dir),
            }
        try:
            result = task.result()
        except Exception as e:  # noqa: BLE001 — surface failure to MCP caller
            return {"run_id": run_id, "status": "error", "error": str(e)}
        return {"run_id": run_id, "status": "done", "result": result}

    # No in-process task — infer from on-disk events / wf-steps.
    events = _tail_events(run_id, journal_dir, limit=5)
    steps_path = Path(journal_dir) / "runs" / run_id / "wf-steps.json"
    if steps_path.exists() and events:
        last = events[-1]
        if last.get("event") in {"done", "exhausted"}:
            return {"run_id": run_id, "status": "unknown_done",
                    "events": events,
                    "hint": "in-process task gone; re-run workflow_start or inspect ledger"}
    return {"run_id": run_id, "status": "unknown", "events": events}


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("iworkflow")

    @server.tool()
    def iworkflow_ping() -> dict[str, str]:
        """Liveness check for the iworkflow engine."""
        return ping()

    @server.tool()
    def iworkflow_list_workflows() -> list[dict[str, Any]]:
        """List the predefined workflow recipes (built-in + host-registered) with
        their parameters, so you can pick one by name for `iworkflow_workflow`."""
        return list_recipes()

    @server.tool()
    async def iworkflow_workflow_start(
        goal: str | None = None,
        workflow: str | None = None,
        params: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
        run_id: str = "mcp",
        cwd: str | None = None,
        timeout_s: float = 180,
    ) -> dict[str, Any]:
        """Start a long-running workflow without blocking the MCP client."""
        return await workflow_start(
            goal, workflow=workflow, params=params, spec=spec, run_id=run_id,
            cwd=cwd, timeout_s=timeout_s,
        )

    @server.tool()
    async def iworkflow_workflow_poll(run_id: str) -> dict[str, Any]:
        """Poll a workflow started with iworkflow_workflow_start."""
        return await workflow_poll(run_id)

    @server.tool()
    async def iworkflow_workflow(goal: str | None = None, workflow: str | None = None,
                                 params: dict[str, Any] | None = None,
                                 spec: dict[str, Any] | None = None,
                                 run_id: str = "mcp",
                                 cwd: str | None = None,
                                 timeout_s: float = 180) -> dict[str, Any]:
        """Run a subscription-only multi-agent workflow across your Codex/Gemini/Claude
        CLIs. Pass `spec` to DEFINE your own dynamic workflow (steps: agent/parallel/
        pipeline/loop), or `workflow`+`params` to run a predefined recipe (see
        iworkflow_list_workflows), or just `goal` for a quick fan→synthesize.

        For runs that may exceed the MCP client's tool timeout, prefer
        iworkflow_workflow_start + iworkflow_workflow_poll."""
        return await run_workflow(
            goal, workflow=workflow, params=params, spec=spec, run_id=run_id,
            cwd=cwd, timeout_s=timeout_s,
        )

    server.run()


if __name__ == "__main__":
    main()

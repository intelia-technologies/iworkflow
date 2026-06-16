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

from typing import Any

from .providers import ClaudeInteractiveProvider, CodexProvider, GeminiProvider
from .recipes import get_recipe, list_recipes
from .scheduler import Runner
from .workflow import run_spec


def ping() -> dict[str, str]:
    """Cheap liveness tool — proves an MCP client can reach iworkflow."""
    return {"ok": "pong from iworkflow", "engine": "subscription-only multi-agent"}


def _default_runner(run_id: str) -> Runner:
    return Runner(run_id, {
        "codex": CodexProvider("codex", timeout_s=180),
        "gemini": GeminiProvider("gemini", timeout_s=180),
        "claude": ClaudeInteractiveProvider("claude", timeout_s=180),
    }, caps={"codex": 2, "gemini": 2, "claude": 1})


async def run_workflow(goal: str | None = None, *, workflow: str | None = None,
                       params: dict[str, Any] | None = None,
                       spec: dict[str, Any] | None = None,
                       run_id: str = "mcp", recipe_dir: str | None = None,
                       runner: Runner | None = None) -> dict[str, Any]:
    """Run a subscription-only multi-agent workflow. Three ways to drive it:

    - `spec=`     : a declarative workflow spec (define your own — DYNAMIC door).
    - `workflow=` : a named recipe (built-in or host-registered) + `params`.
    - `goal=`     : sugar for the `fan_synthesize` recipe over a single question.

    `runner` is injectable so tests pass a FakeProvider-backed Runner (no quota).
    """
    r = runner or _default_runner(run_id)
    if spec is not None:
        return await run_spec(r, spec, params)
    if workflow is not None:
        return await run_spec(r, get_recipe(workflow, recipe_dir), params)
    if goal is not None:
        return await run_spec(r, get_recipe("fan_synthesize"), {"goal": goal})
    raise ValueError("provide one of: spec, workflow, or goal")


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
    async def iworkflow_workflow(goal: str | None = None, workflow: str | None = None,
                                 params: dict[str, Any] | None = None,
                                 spec: dict[str, Any] | None = None,
                                 run_id: str = "mcp") -> dict[str, Any]:
        """Run a subscription-only multi-agent workflow across your Codex/Gemini/Claude
        CLIs. Pass `spec` to DEFINE your own dynamic workflow (steps: agent/parallel/
        pipeline/loop), or `workflow`+`params` to run a predefined recipe (see
        iworkflow_list_workflows), or just `goal` for a quick fan→synthesize."""
        return await run_workflow(goal, workflow=workflow, params=params, spec=spec,
                                  run_id=run_id)

    server.run()


if __name__ == "__main__":
    main()

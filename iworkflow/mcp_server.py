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
from .scheduler import Runner


def ping() -> dict[str, str]:
    """Cheap liveness tool — proves an MCP client can reach iworkflow."""
    return {"ok": "pong from iworkflow", "engine": "subscription-only multi-agent"}


def _default_runner(run_id: str) -> Runner:
    return Runner(run_id, {
        "codex": CodexProvider("codex", timeout_s=180),
        "gemini": GeminiProvider("gemini", timeout_s=180),
        "claude": ClaudeInteractiveProvider("claude", timeout_s=180),
    }, caps={"codex": 2, "gemini": 2, "claude": 1})


async def run_workflow(goal: str, run_id: str = "mcp",
                       runner: Runner | None = None) -> dict[str, Any]:
    """Drive a subscription-only fan→synthesize workflow over `goal`.

    Two proposers (different strengths) + one synthesizer. `runner` is injectable
    so tests can pass a FakeProvider-backed Runner (no quota)."""
    r = runner or _default_runner(run_id)
    fan = await r.parallel([
        lambda: r.agent(f"From an engineering angle, answer concisely: {goal}",
                        label="propose:eng", prefer=["codex", "gemini"]),
        lambda: r.agent(f"From a product/UX angle, answer concisely: {goal}",
                        label="propose:dx", prefer=["gemini", "codex"]),
    ])
    proposals = [p.value for p in fan if p and p.ok]
    synth = await r.agent(
        f"Synthesize ONE decisive answer to: {goal}\nInputs: {proposals}",
        label="synth", prefer=["codex", "claude"])
    return {
        "goal": goal,
        "answer": synth.value if synth.ok else None,
        "proposals": proposals,
        "providers": [p.provider for p in fan] + [synth.provider],
    }


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("iworkflow")

    @server.tool()
    def iworkflow_ping() -> dict[str, str]:
        """Liveness check for the iworkflow engine."""
        return ping()

    @server.tool()
    async def iworkflow_workflow(goal: str, run_id: str = "mcp") -> dict[str, Any]:
        """Run a subscription-only multi-agent iworkflow over `goal` (fan→synthesize)
        across your Codex/Gemini/Claude CLIs and return the synthesized answer."""
        return await run_workflow(goal, run_id=run_id)

    server.run()


if __name__ == "__main__":
    main()

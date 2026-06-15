#!/usr/bin/env python3
"""Use iworkflow FOR REAL: the 3 subscription agents draft iworkflow's roadmap.

A genuine multi-agent task (not a self-review): each provider proposes from its
strength, then one synthesizes a prioritized roadmap saved to docs/ROADMAP.md.

  fan  → Codex (engineering robustness)  ‖  Gemini (capabilities / DX)
  synth→ Claude (reasoning), failover Codex — merge + prioritize

Lean context (a summary, not full source) to stay quota-friendly. Degrades
gracefully if a subscription is throttled (writes whatever it got).

Run:  python3 examples/workflow_roadmap.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import (  # noqa: E402
    ClaudeInteractiveProvider, CodexProvider, GeminiProvider, Runner, log,
)

CONTEXT = """iworkflow is a deterministic, subscription-only orchestrator that drives
coding-agent CLIs (codex exec, agy/Gemini, interactive claude via tmux) as workers.
Zero API tokens; orchestration is plain Python (zero coordination tokens).
DONE: per-provider concurrency semaphore; cross-subscription failover; journal
resume; native/fallback structured output; capability-aware default routing;
parallel() barrier and pipeline() (per-item staged) primitives.
NOT DONE: MCP-server face (so Codex/Gemini can DRIVE workflows as a tool); empirical
routing (learn best provider per task-kind from journal outcomes); worktree-per-agent
isolation for parallel writers; richer per-CLI rate-limit detection + resume-after-
throttle as a first-class loop; a progress TUI; tmux backend hardening for WRITE workers."""

PROPOSAL = {
    "type": "object", "additionalProperties": False, "required": ["proposals"],
    "properties": {"proposals": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "rationale", "effort"],
        "properties": {"title": {"type": "string"}, "rationale": {"type": "string"},
                       "effort": {"type": "string", "enum": ["S", "M", "L"]}}}}},
}


async def main():
    runner = Runner(
        run_id="roadmap-1",
        providers={"codex": CodexProvider("codex", timeout_s=180),
                   "gemini": GeminiProvider("gemini", timeout_s=180),
                   "claude": ClaudeInteractiveProvider("claude", timeout_s=180)},
        caps={"codex": 2, "gemini": 2, "claude": 1})

    log("PHASE propose")
    fan = await runner.parallel([
        lambda: runner.agent(
            CONTEXT + "\n\nAs an engineering-robustness reviewer, propose the 3 "
            "highest-impact NEXT improvements (correctness, reliability, testing).",
            label="propose:eng", schema=PROPOSAL, prefer=["codex", "gemini"]),
        lambda: runner.agent(
            CONTEXT + "\n\nAs a capabilities/DX reviewer, propose the 3 most "
            "valuable NEXT improvements (new primitives, ergonomics, adoption).",
            label="propose:dx", schema=PROPOSAL, prefer=["gemini", "codex"]),
    ])
    proposals = [p.value for p in fan if p and p.ok]
    for p in fan:
        print(f"PROPOSE [{p.provider}] {p.label} → {p.value}")

    log("PHASE synthesize")
    synth = await runner.agent(
        "You are a tech lead. Merge these proposals into ONE prioritized roadmap "
        "for iworkflow (rank, title, one-line why, effort S/M/L). Be decisive; "
        "dedupe; put the highest-leverage item first. Proposals:\n"
        + str(proposals),
        label="synthesize", prefer=["claude", "codex"])     # reasoning → Claude
    print(f"\nSYNTH [{synth.provider}] →\n{synth.value}")

    # write the artifact (whatever we got)
    out = ROOT / "docs" / "ROADMAP.md"
    out.parent.mkdir(exist_ok=True)
    body = synth.value if synth.ok else "(synthesis unavailable — providers throttled)"
    out.write_text(
        "# iworkflow roadmap\n\n"
        f"_Drafted by a real iworkflow run (roadmap-1): Codex + Gemini proposed, "
        f"{synth.provider or 'none'} synthesized._\n\n"
        f"{body}\n\n---\n\n## Raw proposals\n\n```\n{proposals}\n```\n")
    print(f"\n→ wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())

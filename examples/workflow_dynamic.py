#!/usr/bin/env python3
"""The "define your own workflow" door — a DYNAMIC declarative spec, run deterministically.

No quota: a scripted provider stands in for the CLIs so the shape is provable
offline (like demo_fakes.py). The same `spec` dict is exactly what an agent would
hand `iworkflow_workflow(spec=...)` over MCP — data, not code.

Shape: gate → (agent-decided) loop. The loop keeps finding new issues until a
CRITIC agent says STOP (or the hard max_iterations cap trips). The critic's
`missing` feeds the next round; dedup keeps the accumulator clean.

Run:  python3 examples/workflow_dynamic.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import Provider, Runner, run_spec  # noqa: E402

# A deterministic stand-in: gate says DONE, finders return new issues, the critic
# says CONTINUE once then STOP (so the loop self-terminates before the cap).
_STATE = {"decisions": 0}


def _responder(prompt: str, schema: dict | None, call: int):
    props = (schema or {}).get("properties", {})
    enum = set(props.get("verdict", {}).get("enum", []))
    if enum == {"DONE", "BLOCKED"}:                       # the gate
        return {"verdict": "DONE", "summary": "sound enough to review"}
    if enum == {"CONTINUE", "STOP"}:                      # the critic / decider
        _STATE["decisions"] += 1
        stop = _STATE["decisions"] >= 2
        return {"verdict": "STOP" if stop else "CONTINUE",
                "missing": [] if stop else ["error paths", "concurrency"]}
    if "findings" in props:                               # a finder
        return {"findings": [{"title": f"issue-{call}"}]}
    return f"text-{call}"


class ScriptedProvider(Provider):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._n = 0

    async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None):
        self.last_usage = {"output_tokens": 5}
        n, self._n = self._n, self._n + 1
        return _responder(prompt, schema, n)


SPEC = {
    "name": "deep-review-dynamic",
    "params": {"subject": None},
    "schemas": {
        "gate": {"type": "object", "required": ["verdict"], "properties": {
            "verdict": {"type": "string", "enum": ["DONE", "BLOCKED"]},
            "summary": {"type": "string"}}},
        "findings": {"type": "object", "properties": {"findings": {"type": "array"}}},
        "decision": {"type": "object", "required": ["verdict"], "properties": {
            "verdict": {"type": "string", "enum": ["CONTINUE", "STOP"]},
            "missing": {"type": "array"}}},
    },
    "steps": [
        {"id": "gate", "kind": "agent", "schema": "gate", "prefer": ["claude", "codex"],
         "gate": {"field": "verdict", "abort_on": "BLOCKED"},
         "prompt": "Assess whether {{params.subject}} is sound enough to review."},
        {"id": "sweep", "kind": "loop", "needs": ["gate"], "max_iterations": 4,
         "until": {"agent": {
             "prompt": "Found so far: {{loop.collected}}. Complete? verdict=STOP if "
                       "nothing material is missing, else CONTINUE and list missing.",
             "stop_when": "STOP", "prefer": ["gemini", "claude"]}},
         "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
         "body": [
             {"id": "find", "kind": "agent", "schema": "findings", "prefer": ["codex"],
              "prompt": "Find NEW issues in {{params.subject}} not in {{loop.collected}}; "
                        "focus on {{loop.decision.missing}}."},
         ]},
    ],
    "output": {"gate": "{{steps.gate.value.verdict}}", "issues": "{{steps.sweep.value}}"},
}


async def main() -> None:
    p = ScriptedProvider("codex")
    runner = Runner("dynamic-demo", {"codex": p, "gemini": p, "claude": p},
                    {"codex": 3, "gemini": 3, "claude": 3}, journal_dir=".iworkflow")
    result = await run_spec(runner, SPEC, {"subject": "the scheduler"})
    print(json.dumps(result, indent=2, default=str))
    assert result["status"] == "DONE"
    assert len(result["output"]["issues"]) == 2          # critic stopped the loop at 2 rounds
    print("\nOK — dynamic spec with an agent-decided loop ran to completion.")


if __name__ == "__main__":
    asyncio.run(main())

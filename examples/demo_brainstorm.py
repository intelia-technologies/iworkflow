#!/usr/bin/env python3
"""Interactive brainstorm workflow demo.

Demonstrates the 8-phase brainstorm recipe using a simulated provider
so you can test the entire interactive process offline and for free.

Run:  python3 examples/demo_brainstorm.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from iworkflow import (  # noqa: E402
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    Limits,
    Provider,
    Runner,
    run_spec,
)
from iworkflow.recipes import get_recipe  # noqa: E402
from iworkflow.toolsets import ToolSet  # noqa: E402


class DemoBrainstormProvider(Provider):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.iterations = 0

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str,
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
    ) -> Any:
        self.last_usage = {"input_tokens": 12, "output_tokens": 24, "cost_usd": None}

        # Simulate answers based on keywords in prompts. More specific phases must
        # come first because later prompts embed prior phase text verbatim.
        if "targeted clarification questions" in prompt:
            print("[SIMULATOR] Phase 2: Generating clarification questions...")
            return {
                "questions": [
                    "1. ¿Qué motor de almacenamiento principal se utilizará (PostgreSQL o SQLite)?",
                    "2. ¿Cuál es la escala de concurrencia esperada para el MVP?"
                ]
            }

        if "existing brainstorms" in prompt:
            print("[SIMULATOR] Phase 1: Searching existing brainstorms...")
            return "No existing brainstorms found in openspec/changes/."

        if "Analyze existing code" in prompt:
            print("[SIMULATOR] Phase 3: Inspecting code context...")
            return "Analyzed 12 files. Found references to sqlite3 in db/connection.py."

        if "Extract relevant 'learnings'" in prompt:
            print("[SIMULATOR] Phase 3: Inspecting rules and learnings...")
            return "Rule found: Prefer stdlib-only core for workflow orchestrators."

        if "propose 2-3 technical approaches" in prompt:
            print("[SIMULATOR] Phase 4: Proposing technical approaches...")
            return {
                "proposals": [
                    {
                        "title": "Option A: SQLite with WAL mode",
                        "pros": ["Simple setup", "Zero infrastructure overhead", "Fast reads"],
                        "cons": ["Limited write concurrency", "Requires local storage"]
                    },
                    {
                        "title": "Option B: PostgreSQL",
                        "pros": ["High write concurrency", "Robust scaling", "Rich feature set"],
                        "cons": ["Infrastructure overhead", "Needs external database setup"]
                    }
                ]
            }

        if "Return STOP if selected approach" in prompt:
            print("[SIMULATOR] Phase 5 (Until Decider): Evaluation of current state...")
            self.iterations += 1
            # Stop loop on second iteration to simulate scope locking after one user reply.
            if self.iterations >= 2:
                print("[SIMULATOR] Verdict: STOP (Scope Locked)")
                return {"verdict": "STOP", "missing": []}
            print("[SIMULATOR] Verdict: CONTINUE (Still waiting on user clarification)")
            return {"verdict": "CONTINUE", "missing": ["Confirmar motor de base de datos elegido."]}

        if "Lock the brainstorm scope" in prompt:
            print("[SIMULATOR] Phase 5: Processing user input...")
            return {
                "selected_approach": "Option B: PostgreSQL",
                "constraints": ["Prefer scalability over zero-infra setup"],
                "unresolved_forks": [],
                "next_action": "Write brainstorm.md for PostgreSQL architecture",
            }

        if "openspec/changes" in prompt and "brainstorm.md" in prompt:
            print("[SIMULATOR] Phase 6: Writing brainstorm specification document...")
            if cwd:
                artifact = Path(cwd) / "openspec/changes/feature-x/brainstorm.md"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("# Brainstorm: feature-x\n", encoding="utf-8")
            return "Successfully generated brainstorm.md in new branch."

        if "thoughts/shared/wiki" in prompt:
            print("[SIMULATOR] Phase 7: Updating thoughts/shared/wiki/...")
            return "Wiki updated with architectural decision: PostgreSQL adopted."

        if "Confirm files created" in prompt:
            print("[SIMULATOR] Phase 8: Handoff and next steps...")
            return "Handoff ready. Run '/workflows:plan feature-x' as the next step."

        if "reached max iterations" in prompt:
            print("[SIMULATOR] Supervisor check: Reviewing loop state...")
            return {"action": "continue"}

        return {"status": "UNKNOWN_PROMPT"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run brainstorm workflow recipe demo.")
    parser.add_argument("--live", action="store_true", help="Run with live subscription CLIs.")
    args = parser.parse_args()

    print("\n==================================================")
    print("Running INTERACTIVE BRAINSTORM Recipe Simulation")
    print("==================================================")

    demo_root = Path(".iworkflow_brainstorm_demo_root")
    journal_dir = str(demo_root / ".iworkflow")
    shutil.rmtree(demo_root, ignore_errors=True)
    demo_root.mkdir(parents=True)

    if args.live:
        providers = {
            "codex": CodexProvider("codex"),
            "gemini": GeminiProvider("gemini"),
            "claude": ClaudeProvider("claude"),
        }
    else:
        p = DemoBrainstormProvider("demo-provider")
        providers = {"codex": p, "gemini": p, "claude": p}

    runner = Runner(
        run_id="brainstorm_demo",
        providers=providers,
        caps={"codex": 2, "gemini": 2, "claude": 2},
        journal_dir=journal_dir,
        default_cwd=str(demo_root),
    )

    try:
        spec = get_recipe("brainstorm")
        # Simulate params passed to iworkflow CLI
        result = await run_spec(
            runner,
            spec,
            params={
                "change_name": "feature-x",
                "user_input": "Prefiero la opción B (PostgreSQL) para asegurar el escalado."
            },
            limits=Limits(allow_tools=True, allowed_sandboxes=["read-only", "write"]),
            preflight_checked=not args.live,
        )
        print("\nWorkflow Run Finished Successfully!")
        print("Final Output Bundle:")
        print(json.dumps(result["output"], indent=2))
        print("\nAll Steps Executed:")
        for sid, val in result["steps"].items():
            print(f"  • {sid:<25}: {str(val)[:80]}...")
    finally:
        shutil.rmtree(demo_root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""End-to-end firepower test: a real gate→fan→audit workflow over 3 subscriptions.

Dogfoods iworkflow by reviewing its OWN source (read-only), exercising the full
Fable-style shape with mixed providers:

  gate   → Claude (core, Pool-1 via tmux)  — assess design; BLOCKED aborts
  fan    → Codex reviews scheduler.py  ‖  Gemini reviews providers.py  (disjoint)
  audit  → Gemini, adversarial, schema-LESS, NO fan results (independence)

All workers subscription-only. Claude capped at 1 (shared weekly limit).

Run:  python3 examples/workflow_review.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import (  # noqa: E402
    ClaudeInteractiveProvider, CodexProvider, GeminiProvider, Runner, log,
)

SCHED = (ROOT / "iworkflow" / "scheduler.py").read_text()
PROV = (ROOT / "iworkflow" / "providers.py").read_text()

COMMON = (
    "You are a reviewer in an iworkflow run. READ-ONLY: do not write files or run "
    "commands. iworkflow is a deterministic, subscription-only orchestrator that "
    "spawns coding-agent CLIs as workers with a per-provider semaphore, "
    "cross-subscription failover, and journal-based resume.\n"
)

GATE = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary", "risks"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "BLOCKED"]},
                   "summary": {"type": "string"},
                   "risks": {"type": "array", "items": {"type": "string"}}},
}
REVIEW = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "severity", "findings"],
    "properties": {"verdict": {"type": "string", "enum": ["PASS", "ISSUES"]},
                   "severity": {"type": "string", "enum": ["low", "med", "high"]},
                   "findings": {"type": "array", "items": {"type": "string"}}},
}


async def main():
    runner = Runner(
        run_id="self-review-1",
        providers={"codex": CodexProvider("codex", timeout_s=180),
                   "gemini": GeminiProvider("gemini", timeout_s=180),
                   "claude": ClaudeInteractiveProvider("claude", timeout_s=180)},
        caps={"codex": 2, "gemini": 2, "claude": 1},   # claude scarce → cap 1
    )

    # ── GATE (Claude core, Pool-1) — fail over to codex if Claude is throttled ──
    log("PHASE gate")
    gate = await runner.agent(
        COMMON + "Assess whether this orchestrator's overall design (per-provider "
        "semaphore + failover + journal resume) is sound enough to review in "
        "detail. verdict=DONE to proceed, BLOCKED if fundamentally broken. "
        "risks = up to 2 short strings.",
        label="gate", schema=GATE, prefer=["claude", "codex"])
    print(f"\nGATE [{gate.provider}] → {gate.value}")
    if not gate.ok or (gate.value or {}).get("verdict") == "BLOCKED":
        print("ABORTED at gate."); return

    # ── FAN (disjoint files, parallel) — Codex ‖ Gemini ──
    log("PHASE fan")
    fan = await runner.parallel([
        lambda: runner.agent(
            COMMON + "Review this module for CORRECTNESS of the failover/resume/"
            "semaphore logic. verdict=PASS/ISSUES.\n\n--- scheduler.py ---\n" + SCHED,
            label="review:scheduler", schema=REVIEW, prefer=["codex", "gemini"]),
        lambda: runner.agent(
            COMMON + "Review this module for ROBUSTNESS of the CLI adapters & the "
            "tmux pane extraction / rate-limit detection. verdict=PASS/ISSUES.\n\n"
            "--- providers.py ---\n" + PROV,
            label="review:providers", schema=REVIEW, prefer=["gemini", "codex"]),
    ])
    for r in fan:
        print(f"FAN  [{r.provider}] {r.label} → {r.value}")

    # ── AUDIT (Gemini, adversarial, schema-LESS, NO fan results) ──
    log("PHASE audit")
    audit = await runner.agent(
        "You are an independent adversarial auditor. Read ONLY the two modules "
        "below and decide: does the failover + journal-resume design have a real "
        "correctness hole (e.g. a throttled provider losing work, or resume "
        "double-running an agent)? Be skeptical; answer in 4 sentences max. Do NOT "
        "trust any other reviewer.\n\n--- scheduler.py ---\n" + SCHED +
        "\n\n--- providers.py ---\n" + PROV,
        label="audit", prefer=["gemini", "codex"])     # schema-less
    print(f"\nAUDIT [{audit.provider}] →\n{audit.value}")

    print("\n" + "═" * 60)
    print("BUNDLE:", json.dumps({
        "gate": gate.value, "fan": [r.value for r in fan],
        "audit_provider": audit.provider}, ensure_ascii=False)[:400])


if __name__ == "__main__":
    asyncio.run(main())

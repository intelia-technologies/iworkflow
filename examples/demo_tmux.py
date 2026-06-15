#!/usr/bin/env python3
"""Live validation of the Pool-1 Claude worker (interactive TUI via tmux).

Uses a MULTI-LINE prompt (COMMON-style contract + task) to also validate
bracketed-paste sending. Spends a little of the Claude WEEKLY subscription
limit (Pool 1), not the Pool-2 credit.

Run:  python3 examples/demo_tmux.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from iworkflow import ClaudeInteractiveProvider  # noqa: E402

RESULT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary", "files_touched"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "summary": {"type": "string"},
                   "files_touched": {"type": "array", "items": {"type": "string"}}},
}

# Multi-line, COMMON-style — validates bracketed paste of a real worker prompt.
PROMPT = """You are a worker in an iworkflow run.
WORKTREE: /tmp — READ-ONLY, touch nothing, run no commands.
TASK: state whether a FastAPI + Temporal backend benefits from durable workflows.
Set verdict=DONE, summary to a six-word phrase, files_touched to an empty array."""


async def main():
    prov = ClaudeInteractiveProvider("claude", timeout_s=150)
    print("Driving interactive Claude via tmux (Pool 1, multi-line prompt)…\n")
    try:
        val = await prov.run(PROMPT, schema=RESULT, sandbox="read-only")
        print(f"✅ claude (tmux/Pool 1) → {val}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ claude (tmux) → {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())

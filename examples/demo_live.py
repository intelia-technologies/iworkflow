#!/usr/bin/env python3
"""Live smoke — one trivial call per SUBSCRIPTION CLI to prove the adapters.

Spends a tiny bit of each subscription (no API tokens). Each provider gets ONE
read-only structured call. Failures are reported, not fatal — the point is to
see which real adapters round-trip cleanly.

Run:  python3 demo_live.py            # all three
      python3 demo_live.py codex      # subset
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from iworkflow import ClaudeProvider, CodexProvider, GeminiProvider  # noqa: E402

RESULT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary", "files_touched"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "summary": {"type": "string"},
                   "files_touched": {"type": "array", "items": {"type": "string"}}},
}
PROMPT = ("Return a structured verdict. Do NOT write any files. verdict=DONE, "
          "summary = a 5-word phrase describing a FastAPI+Temporal backend, "
          "files_touched = []. Respond only via the schema.")

PROVIDERS = {
    "codex":  CodexProvider("codex", timeout_s=150),
    "claude": ClaudeProvider("claude", timeout_s=150),
    "gemini": GeminiProvider("gemini", timeout_s=150),
}


async def one(name):
    prov = PROVIDERS[name]
    try:
        val = await prov.run(PROMPT, schema=RESULT, sandbox="read-only")
        print(f"✅ {name:7} → {val}")
        return True
    except Exception as e:  # noqa: BLE001 — smoke wants the reason, not a crash
        print(f"❌ {name:7} → {type(e).__name__}: {str(e)[:160]}")
        return False


async def main():
    which = sys.argv[1:] or list(PROVIDERS)
    print(f"Live smoke across: {which}\n")
    results = await asyncio.gather(*(one(n) for n in which))
    print(f"\n{sum(results)}/{len(results)} adapters round-tripped clean.")


if __name__ == "__main__":
    asyncio.run(main())

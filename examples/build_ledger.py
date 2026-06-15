#!/usr/bin/env python3
"""Orchestrate the build of feature #1 (durable run ledger) WITH iworkflow.

Dogfood: iworkflow builds iworkflow.
  implement → Codex (workspace-write, SINGLE writer) creates iworkflow/ledger.py
  audit     → Gemini (read-only) reviews the new module adversarially

Additive only (a new file) so it can't break the existing 5/5. The orchestrator
(parent) wires it in + runs the suite afterwards — never trusts the worker.

Run from the repo root on the feat/durable-ledger branch.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import CodexProvider, GeminiProvider, Runner, log  # noqa: E402

SPEC = """Create a NEW file iworkflow/ledger.py — DO NOT modify any other file.
Stdlib-only (no new deps). It implements a durable, crash-safe run ledger that
supersedes the inline journal in scheduler.py. Requirements:

class LedgerRecord (a dataclass): run_id, label, status, provider, value (Any),
  attempts (list of dicts: provider, outcome, latency_ms), prompt_sha (str),
  schema_sha (str|None), ts_start (float), ts_end (float), error_class (str|None),
  retry_after (float|None).

class RunLedger:
  __init__(self, run_id: str, dir: str = ".iworkflow")
    -> file at <dir>/runs/<run_id>/ledger.jsonl, parents created.
  append(self, rec: LedgerRecord) -> None
    -> append one JSON line; crash-safe (write+flush+os.fsync; or temp-rewrite).
  load_done(self) -> dict[str, dict]
    -> map label -> the record dict for records whose status == "DONE";
       MUST skip malformed/partial lines without raising (json.JSONDecodeError).
  record_cooldown(self, provider: str, until_ts: float) -> None
  is_cooling(self, provider: str, now: float) -> bool
    -> cooldown persisted in <dir>/runs/<run_id>/cooldown.json; tracks per-provider
       'do not dispatch until' timestamps so a throttled provider is skipped.
  Helpers: sha for prompt/schema (hashlib.sha256 hexdigest, truncated to 16).

Keep it small, typed (X | None, list[...], dict[...]), documented. Add a tiny
`if __name__ == '__main__':` self-test that appends 2 records (one DONE, one
EXHAUSTED), reloads, and asserts load_done() returns exactly the DONE one and
that a malformed appended line is skipped. Print 'ledger self-test OK'."""

REPORT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "files_created", "summary"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "files_created": {"type": "array", "items": {"type": "string"}},
                   "summary": {"type": "string"}},
}


async def main():
    runner = Runner(
        run_id="build-ledger-1",
        providers={"codex": CodexProvider("codex", timeout_s=300),
                   "gemini": GeminiProvider("gemini", timeout_s=200)},
        caps={"codex": 1, "gemini": 1})        # single writer

    log("PHASE implement (codex, workspace-write)")
    impl = await runner.agent(
        SPEC + "\n\nImplement it now, then run your self-test.",
        label="implement:ledger", schema=REPORT,
        prefer=["codex"], sandbox="workspace-write")
    print(f"\nIMPLEMENT [{impl.provider}] → {impl.value}")

    log("PHASE audit (gemini, read-only)")
    audit = await runner.agent(
        "Read the file iworkflow/ledger.py in this repo and review it adversarially: "
        "is the crash-safe append real? does load_done skip partial lines? is cooldown "
        "persisted correctly? any bug that would break resume? Answer in 5 sentences max.",
        label="audit:ledger", prefer=["gemini", "codex"])
    print(f"\nAUDIT [{audit.provider}] →\n{audit.value}")

    return {"implement": impl.value, "audit": audit.value}


if __name__ == "__main__":
    asyncio.run(main())

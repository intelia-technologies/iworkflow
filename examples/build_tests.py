#!/usr/bin/env python3
"""Orchestrate building the fault-injection test suite (roadmap #2) WITH iworkflow.

  implement → Codex (workspace-write, single) creates tests/ (pytest, stdlib-only)
  audit     → Gemini (read-only) checks the tests actually assert behaviour

Parent verifies by RUNNING pytest in .venv and spot-checking the tests aren't
vacuous. Subscription-free at run time (FakeProvider only).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import CodexProvider, GeminiProvider, Runner, log  # noqa: E402

SPEC = """Create a pytest test suite under tests/ for the iworkflow package.
DO NOT modify any file outside tests/. Stdlib + pytest only (NO pytest-asyncio):
call async methods with `asyncio.run(...)` inside plain `def test_*` functions.
Use the `tmp_path` fixture for any journal/ledger dir (pass journal_dir=str(tmp_path)).
Use iworkflow.FakeProvider so NO real CLI is ever spawned. Make assertions REAL —
they must fail if the behaviour breaks. Cover, in separate files:

tests/test_scheduler.py
  - per-provider semaphore cap: FakeProvider(delay_s=0.1), cap=2, fan 6 via
    parallel(), assert provider.concurrent_peak <= 2.
  - failover order: codex FakeProvider(limit_first_n=99) + gemini healthy,
    role='doer'; assert result.provider == 'gemini' and the attempts list is
    [('codex','RATE_LIMITED'),('gemini','DONE')] by provider/outcome.
  - exhausted: all providers limit_first_n=99; assert status=='EXHAUSTED',
    value is None, ok is False.
  - within-process dedup: same label twice; assert provider called once and the
    second AgentResult.resumed is True.
  - cross-runner resume: run 3 labels, build a NEW Runner same run_id + a fresh
    FakeProvider; assert the fresh provider is called 0 times and results resumed.

tests/test_ledger.py
  - append two records (DONE + EXHAUSTED), append a malformed line manually,
    assert load_done() returns only the DONE label and does not raise.
  - record_cooldown/is_cooling across a fresh RunLedger (persistence) using
    explicit now timestamps (before and after until_ts).
  - after a cooldown write, assert no leftover .tmp file remains in the run dir.

tests/test_routing.py
  - infer_kind: assert sweep (huge prompt), audit, write, implement, classify,
    'structured' (schema given, no keyword), 'default' (nothing).
  - route(): explicit role honored; available filtering (provider not present is
    dropped); a schema-only prompt routes codex-first.

tests/test_providers.py
  - Provider._classify: exit 0 with text containing 'rate limit and quota' does
    NOT raise (the regression); exit 1 + 'usage limit reached' raises RateLimited;
    exit 1 + 'boom' raises ProviderError; exit 124 raises ProviderError.
  - iworkflow.providers._find_schema_json extracts the last schema-valid object
    from noisy pane text; iworkflow.providers._response_text strips ⏺/box chrome.

tests/test_pipeline.py
  - pipeline over 3 items through 2 FakeProvider stages → 3 non-None results.
  - a stage that raises drops THAT item to None while others succeed.

tests/test_minijsonschema.py
  - validate(): required missing → (False,..); enum violation → False;
    additionalProperties false + extra key → False; happy path → True.

Run `python -m pytest -q` yourself and ensure it is green before reporting."""

REPORT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "files_created", "summary"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "files_created": {"type": "array", "items": {"type": "string"}},
                   "summary": {"type": "string"}},
}


async def main():
    runner = Runner(
        run_id="build-tests-1",
        providers={"codex": CodexProvider("codex", timeout_s=420),
                   "gemini": GeminiProvider("gemini", timeout_s=200)},
        caps={"codex": 1, "gemini": 1})

    log("PHASE implement tests (codex, workspace-write)")
    impl = await runner.agent(
        SPEC, label="implement:tests", schema=REPORT,
        prefer=["codex"], sandbox="workspace-write")
    print(f"\nIMPLEMENT [{impl.provider}] → {impl.value}")

    log("PHASE audit (gemini, read-only)")
    audit = await runner.agent(
        "Read the files under tests/ in this repo. Are these tests REAL — do they "
        "assert specific behaviour that would fail if the code regressed, or are any "
        "vacuous (e.g. assert True, no assert, only smoke imports)? Name any weak "
        "test. 6 sentences max.",
        label="audit:tests", prefer=["gemini", "codex"])
    print(f"\nAUDIT [{audit.provider}] →\n{audit.value}")
    return {"implement": impl.value, "audit": audit.value}


if __name__ == "__main__":
    asyncio.run(main())

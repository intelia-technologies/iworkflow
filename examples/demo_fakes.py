#!/usr/bin/env python3
"""Deterministic proof of the scheduler — no quota spent, no CLIs called.

Proves the three differentiated behaviours against FakeProviders:
  1. per-provider concurrency cap is honored (semaphore)
  2. failover: when one subscription is throttled, work re-dispatches to another
  3. resume: re-running the same run_id replays completed agents from the journal
           with ZERO new provider calls (survives a rate-limit window / crash)

Run:  python3 demo_fakes.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from iworkflow import FakeProvider, Runner  # noqa: E402

RESULT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary", "provider"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "summary": {"type": "string"}, "provider": {"type": "string"}},
}
JOURNAL = ".iworkflow_test"


def fresh():
    shutil.rmtree(JOURNAL, ignore_errors=True)


async def scenario_1_cap() -> bool:
    print("\n── Scenario 1: per-provider concurrency cap ─────────────")
    codex = FakeProvider("codex", delay_s=0.15)
    r = Runner("cap", {"codex": codex}, caps={"codex": 2}, journal_dir=JOURNAL)
    await r.parallel([
        (lambda i=i: r.agent(f"task {i}", label=f"t{i}", schema=RESULT, role="doer"))
        for i in range(6)
    ])
    ok = codex.concurrent_peak <= 2
    print(f"   codex concurrent_peak={codex.concurrent_peak} (cap=2) → {'PASS' if ok else 'FAIL'}")
    return ok


async def scenario_2_failover() -> bool:
    print("\n── Scenario 2: failover BETWEEN subscriptions (codex→gemini) ──")
    # Subscription-only: Codex throttled, Gemini healthy. role=doer → [codex, gemini].
    codex = FakeProvider("codex", limit_first_n=99)
    gemini = FakeProvider("gemini")
    r = Runner("failover", {"codex": codex, "gemini": gemini},
               caps={"codex": 4, "gemini": 4}, journal_dir=JOURNAL)
    results = await r.parallel([
        (lambda i=i: r.agent(f"doer task {i}", label=f"c{i}", schema=RESULT, role="doer"))
        for i in range(4)
    ])
    all_done = all(x.ok for x in results)
    all_via_gemini = all(x.provider == "gemini" for x in results)
    failed_over = all(
        [a.provider for a in x.attempts] == ["codex", "gemini"] for x in results)
    ok = all_done and all_via_gemini and failed_over
    print(f"   4/4 DONE={all_done}  via gemini={all_via_gemini}  "
          f"codex→gemini failover={failed_over} → {'PASS' if ok else 'FAIL'}")
    return ok


async def scenario_3_resume() -> bool:
    print("\n── Scenario 3: resume from journal (0 new calls) ────────")
    fresh()
    p1 = {"codex": FakeProvider("codex")}
    r1 = Runner("resume-run", p1, caps={"codex": 4}, journal_dir=JOURNAL)
    for i in range(3):
        await r1.agent(f"step {i}", label=f"s{i}", schema=RESULT, role="doer")
    calls_run1 = p1["codex"]._calls

    # Simulate crash / limit-window expiry: brand-new providers + Runner, SAME run_id.
    p2 = {"codex": FakeProvider("codex")}
    r2 = Runner("resume-run", p2, caps={"codex": 4}, journal_dir=JOURNAL)
    results = [await r2.agent(f"step {i}", label=f"s{i}", schema=RESULT, role="doer")
               for i in range(3)]
    calls_run2 = p2["codex"]._calls
    all_resumed = all(x.resumed and x.ok for x in results)
    ok = calls_run1 == 3 and calls_run2 == 0 and all_resumed
    print(f"   run1 calls={calls_run1}  run2 calls={calls_run2} (expect 0)  "
          f"all resumed={all_resumed} → {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    fresh()
    results = [await scenario_1_cap(),
               await scenario_2_failover(),
               await scenario_3_resume()]
    fresh()
    passed = sum(results)
    print(f"\n═══ {passed}/{len(results)} scenarios PASS ═══")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

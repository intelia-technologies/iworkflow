import asyncio

from iworkflow import FakeProvider, Runner
from iworkflow.stats import provider_stats


def test_provider_stats_aggregates_ledger_outcomes(tmp_path):
    codex = FakeProvider("codex", limit_first_n=99)   # always throttles
    gemini = FakeProvider("gemini")                   # always succeeds
    runner = Runner(
        "stats-run",
        {"codex": codex, "gemini": gemini},
        {"codex": 1, "gemini": 1},
        journal_dir=str(tmp_path),
    )
    asyncio.run(runner.agent("implement x", label="j", role="doer"))

    stats = provider_stats(journal_dir=str(tmp_path))

    assert stats["codex"]["rate_limited"] >= 1
    assert stats["codex"]["success_rate"] == 0.0
    assert stats["gemini"]["done"] >= 1
    assert stats["gemini"]["success_rate"] == 1.0


def test_provider_stats_empty_when_no_ledger(tmp_path):
    assert provider_stats(journal_dir=str(tmp_path)) == {}

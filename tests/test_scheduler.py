import asyncio

from iworkflow import FakeProvider, Provider, Runner


class CwdRecordingProvider(Provider):
    def __init__(self):
        super().__init__("codex")
        self.cwd = None

    async def run(self, prompt, *, schema, sandbox="read-only", cwd=None):
        self.cwd = cwd
        return {"verdict": "DONE", "summary": prompt}


def test_per_provider_semaphore_cap(tmp_path):
    provider = FakeProvider("codex", delay_s=0.1)
    runner = Runner(
        "semaphore-cap",
        {"codex": provider},
        {"codex": 2},
        journal_dir=str(tmp_path),
    )

    async def run_batch():
        return await runner.parallel(
            [
                lambda i=i: runner.agent(
                    f"implement task {i}",
                    label=f"task-{i}",
                    role="doer",
                )
                for i in range(6)
            ]
        )

    results = asyncio.run(run_batch())

    assert all(result.ok for result in results)
    assert provider.concurrent_peak <= 2
    assert provider._calls == 6


def test_agent_forwards_cwd_to_provider(tmp_path):
    provider = CwdRecordingProvider()
    runner = Runner(
        "cwd-forwarding",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path / "journal"),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = asyncio.run(
        runner.agent(
            "write in this checkout",
            label="job",
            prefer=["codex"],
            cwd=str(worktree),
        )
    )

    assert result.ok
    assert provider.cwd == str(worktree)


def test_failover_order(tmp_path):
    codex = FakeProvider("codex", limit_first_n=99)
    gemini = FakeProvider("gemini")
    runner = Runner(
        "failover-order",
        {"codex": codex, "gemini": gemini},
        {"codex": 1, "gemini": 1},
        journal_dir=str(tmp_path),
    )

    result = asyncio.run(
        runner.agent("implement the thing", label="job", role="doer")
    )

    assert result.ok
    assert result.provider == "gemini"
    assert [(a.provider, a.outcome) for a in result.attempts] == [
        ("codex", "RATE_LIMITED"),
        ("gemini", "DONE"),
    ]
    assert codex._calls == 1
    assert gemini._calls == 1


def test_exhausted_when_every_provider_limits(tmp_path):
    codex = FakeProvider("codex", limit_first_n=99)
    gemini = FakeProvider("gemini", limit_first_n=99)
    runner = Runner(
        "exhausted",
        {"codex": codex, "gemini": gemini},
        {"codex": 1, "gemini": 1},
        journal_dir=str(tmp_path),
    )

    result = asyncio.run(
        runner.agent("implement the thing", label="job", role="doer")
    )

    assert result.status == "EXHAUSTED"
    assert result.value is None
    assert result.ok is False
    assert result.provider is None
    assert [(a.provider, a.outcome) for a in result.attempts] == [
        ("codex", "RATE_LIMITED"),
        ("gemini", "RATE_LIMITED"),
    ]


def test_within_process_dedup_uses_first_result(tmp_path):
    provider = FakeProvider("codex")
    runner = Runner(
        "within-process-dedup",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )

    first = asyncio.run(runner.agent("same work", label="same", role="doer"))
    second = asyncio.run(runner.agent("same work", label="same", role="doer"))

    assert first.ok
    assert first.resumed is False
    assert second.ok
    assert second.resumed is True
    assert second.value == first.value
    assert provider._calls == 1


def test_cooldown_skips_recently_throttled_provider(tmp_path):
    codex = FakeProvider("codex", limit_first_n=1)   # only the first call throttles
    gemini = FakeProvider("gemini")
    runner = Runner(
        "cooldown",
        {"codex": codex, "gemini": gemini},
        {"codex": 1, "gemini": 1},
        journal_dir=str(tmp_path),
        cooldown_s=300,
    )

    first = asyncio.run(runner.agent("job a", label="a", role="doer"))
    second = asyncio.run(runner.agent("job b", label="b", role="doer"))

    # first agent: codex throttles (records a cooldown) → fails over to gemini
    assert [(a.provider, a.outcome) for a in first.attempts] == [
        ("codex", "RATE_LIMITED"),
        ("gemini", "DONE"),
    ]
    # second agent: codex is still cooling → skipped, NOT dispatched again → gemini
    assert second.provider == "gemini"
    assert [(a.provider, a.outcome) for a in second.attempts] == [
        ("codex", "COOLING"),
        ("gemini", "DONE"),
    ]
    assert codex._calls == 1   # codex was not hammered while throttled


def test_cross_runner_resume_uses_ledger_without_dispatch(tmp_path):
    run_id = "cross-runner-resume"
    first_provider = FakeProvider("codex")
    first_runner = Runner(
        run_id,
        {"codex": first_provider},
        {"codex": 2},
        journal_dir=str(tmp_path),
    )

    async def seed_ledger():
        return await first_runner.parallel(
            [
                lambda i=i: first_runner.agent(
                    f"work {i}",
                    label=f"label-{i}",
                    role="doer",
                )
                for i in range(3)
            ]
        )

    seeded = asyncio.run(seed_ledger())
    assert [result.status for result in seeded] == ["DONE", "DONE", "DONE"]
    assert first_provider._calls == 3

    fresh_provider = FakeProvider("codex")
    fresh_runner = Runner(
        run_id,
        {"codex": fresh_provider},
        {"codex": 2},
        journal_dir=str(tmp_path),
    )

    async def resume_all():
        return await fresh_runner.parallel(
            [
                lambda i=i: fresh_runner.agent(
                    f"work {i}",
                    label=f"label-{i}",
                    role="doer",
                )
                for i in range(3)
            ]
        )

    resumed = asyncio.run(resume_all())

    assert fresh_provider._calls == 0
    assert [result.status for result in resumed] == ["DONE", "DONE", "DONE"]
    assert all(result.resumed for result in resumed)
    assert [result.value for result in resumed] == [result.value for result in seeded]

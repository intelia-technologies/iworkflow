import asyncio
import json
from pathlib import Path

from iworkflow import ClaudeInteractiveProvider, FakeProvider, Provider, Runner


class CwdRecordingProvider(Provider):
    def __init__(self):
        super().__init__("codex")
        self.cwd = None

    async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None, model=None):
        self.cwd = cwd
        return {"verdict": "DONE", "summary": prompt}


class StaticProvider(Provider):
    def __init__(self, name, value):
        super().__init__(name)
        self.value = value

    async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None, model=None):
        return self.value


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


def test_agent_emits_prompt_before_dispatch_and_model_on_done(tmp_path):
    provider = FakeProvider("codex")
    runner = Runner(
        "prompt-event",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )

    result = asyncio.run(
        runner.agent(
            "write a plan for dashboards",
            label="planner",
            prefer=["codex"],
            model="gpt-5.1-codex-mini",
        )
    )

    assert result.ok
    events_path = tmp_path / "runs" / "prompt-event" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["route", "prompt", "dispatch", "done"]
    assert events[1]["label"] == "planner"
    assert events[1]["text"] == "write a plan for dashboards"
    assert events[1]["prompt_sha"]
    assert events[2]["model"] == "gpt-5.1-codex-mini"
    assert events[3]["model"] == "gpt-5.1-codex-mini"


def test_schema_mismatch_event_contains_diagnostic_and_ledger_schema_ok(tmp_path):
    provider = StaticProvider("codex", {"result": "ok"})
    runner = Runner(
        "schema-event",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )
    schema = {"type": "object", "required": ["score"]}

    result = asyncio.run(
        runner.agent("score it", label="judge", prefer=["codex"], schema=schema)
    )

    assert result.status == "EXHAUSTED"
    assert result.schema_ok is False
    events_path = tmp_path / "runs" / "schema-event" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    mismatch = next(event for event in events if event["event"] == "schema_mismatch")
    assert mismatch["label"] == "judge"
    assert mismatch["provider"] == "codex"
    assert mismatch["why"] == "missing required key: 'score'"
    ledger = json.loads((tmp_path / "runs" / "schema-event" / "ledger.jsonl").read_text())
    assert ledger["schema_ok"] is False
    assert ledger["attempts"] == [{
        "provider": "codex",
        "outcome": "SCHEMA_MISMATCH",
        "detail": "missing required key: 'score'",
    }]


def test_provider_side_schema_error_is_schema_mismatch_event(tmp_path):
    provider = FakeProvider("codex")
    runner = Runner(
        "schema-provider-error",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )
    schema = {"type": "object", "required": ["score"]}

    result = asyncio.run(
        runner.agent("score it", label="judge", prefer=["codex"], schema=schema)
    )

    assert result.status == "EXHAUSTED"
    events_path = tmp_path / "runs" / "schema-provider-error" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    mismatch = next(event for event in events if event["event"] == "schema_mismatch")
    assert mismatch["provider"] == "codex"
    assert mismatch["why"] == "missing required key: 'score'"


def test_runner_injects_tmux_socket(tmp_path):
    provider = ClaudeInteractiveProvider("claude")

    runner = Runner(
        "myrun",
        {"claude": provider},
        {"claude": 1},
        journal_dir=str(tmp_path),
    )

    assert runner.providers["claude"].tmux_socket == "iw_myrun"


def test_runner_does_not_add_tmux_socket_to_non_claude_provider(tmp_path):
    fake = FakeProvider("fake")
    claude = ClaudeInteractiveProvider("claude")

    runner = Runner(
        "x",
        {"fake": fake, "claude": claude},
        {"fake": 1, "claude": 1},
        journal_dir=str(tmp_path),
    )

    assert not hasattr(runner.providers["fake"], "tmux_socket")
    assert runner.providers["claude"].tmux_socket == "iw_x"


def test_runner_teardown_tmux_called(tmp_path, monkeypatch):
    captured = []

    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return b"", b"server not found"

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        captured.append(list(argv))
        return FakeProcess()

    monkeypatch.setattr(
        "iworkflow.scheduler.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    runner = Runner(
        "abc",
        {"claude": ClaudeInteractiveProvider("claude")},
        {"claude": 1},
        journal_dir=str(tmp_path),
    )

    asyncio.run(runner.teardown_tmux())

    assert captured == [["tmux", "-L", "iw_abc", "kill-server"]]


def test_runner_teardown_tmux_is_noop_without_claude_interactive(tmp_path, monkeypatch):
    async def fake_create_subprocess_exec(*_argv, **_kwargs):
        raise AssertionError("tmux should not be invoked without a ClaudeInteractiveProvider")

    monkeypatch.setattr(
        "iworkflow.scheduler.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    runner = Runner(
        "no-claude",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )

    asyncio.run(runner.teardown_tmux())



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


def test_resume_invalidates_cache_when_prompt_changes(tmp_path):
    run_id = "prompt-change"
    provider = FakeProvider("codex")
    runner = Runner(
        run_id,
        {"codex": provider},
        {"codex": 2},
        journal_dir=str(tmp_path),
    )

    async def run_once():
        first = await runner.agent("answer A", label="same-label")
        second = await runner.agent("answer B", label="same-label")
        return first, second

    first, second = asyncio.run(run_once())

    assert first.value != second.value
    assert provider._calls == 2
    assert first.resumed is False
    assert second.resumed is False


def test_spill_dispatches_to_idle_provider(tmp_path):
    codex = FakeProvider("codex", delay_s=0.3)
    cursor = FakeProvider("cursor")
    runner = Runner(
        "spill-on",
        {"codex": codex, "cursor": cursor},
        {"codex": 1, "cursor": 2},
        journal_dir=str(tmp_path),
        spill=True,
    )

    async def go():
        # A occupies codex's only slot; with spill, B should dispatch to the idle
        # cursor instead of queueing behind A on the busy higher-priority codex.
        a = asyncio.create_task(runner.agent("a", label="a", prefer=["codex", "cursor"]))
        await asyncio.sleep(0.05)
        b = await runner.agent("b", label="b", prefer=["codex", "cursor"])
        await a
        return b

    b = asyncio.run(go())
    assert b.provider == "cursor"


def test_no_spill_waits_for_priority_provider(tmp_path):
    codex = FakeProvider("codex", delay_s=0.2)
    cursor = FakeProvider("cursor")
    runner = Runner(
        "spill-off",
        {"codex": codex, "cursor": cursor},
        {"codex": 1, "cursor": 2},
        journal_dir=str(tmp_path),
    )  # spill defaults False → strict priority

    async def go():
        a = asyncio.create_task(runner.agent("a", label="a", prefer=["codex", "cursor"]))
        await asyncio.sleep(0.05)
        b = await runner.agent("b", label="b", prefer=["codex", "cursor"])
        await a
        return b

    b = asyncio.run(go())
    assert b.provider == "codex"


def test_spill_never_promotes_scarce_provider(tmp_path):
    codex = FakeProvider("codex", delay_s=0.2)
    claude = FakeProvider("claude")
    runner = Runner(
        "spill-scarce",
        {"codex": codex, "claude": claude},
        {"codex": 1, "claude": 1},
        journal_dir=str(tmp_path),
        spill=True,
    )

    async def go():
        a = asyncio.create_task(runner.agent("a", label="a", prefer=["codex", "claude"]))
        await asyncio.sleep(0.05)
        b = await runner.agent("b", label="b", prefer=["codex", "claude"])
        await a
        return b

    b = asyncio.run(go())
    # claude is high-scarcity → never spill-promoted; B waits for codex instead.
    assert b.provider == "codex"


def test_emit_reopens_closed_events_file(tmp_path):
    runner = Runner(
        "emit-reopen",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )
    runner._emit("a", "first")
    runner._close_events()
    # an emit after teardown (run_workflow's error path) must not crash.
    runner._emit("b", "second")
    path = Path(str(tmp_path)) / "runs" / "emit-reopen" / "events.jsonl"
    events = [json.loads(line)["event"] for line in path.read_text().splitlines() if line.strip()]
    assert "first" in events
    assert "second" in events

import asyncio
import json

import pytest

from iworkflow.mcp_server import (
    check_sessions,
    SYNC_WORKFLOW_DOC,
    _maybe_degrade_fan_synthesize,
    _read_events_since,
    _resolve_catalog,
    _resolve_journal_dir,
    _resolve_run_id,
    _workflow_status,
    run_workflow,
    workflow_poll,
    workflow_start,
    workflow_stream,
)
from iworkflow import FakeProvider, Runner


def test_resolve_journal_dir_relative_to_cwd(tmp_path):
    assert _resolve_journal_dir(".iworkflow", str(tmp_path)) == str(tmp_path / ".iworkflow")
    absolute = tmp_path / "custom-journal"
    assert _resolve_journal_dir(str(absolute), "/elsewhere") == str(absolute)
    assert _resolve_journal_dir(".iworkflow", None) == ".iworkflow"


def test_resolve_run_id_hashes_default_mcp():
    a = _resolve_run_id("mcp", "goal one", None)
    b = _resolve_run_id("mcp", "goal two", None)
    c = _resolve_run_id("custom", "goal one", None)
    assert a != b
    assert a.startswith("mcp-")
    assert c == "custom"


def test_maybe_degrade_fan_synthesize_uses_best_proposal():
    result = {
        "name": "fan_synthesize",
        "output": None,
        "steps": {"fan": ["short", "much longer proposal text"]},
    }
    degraded = _maybe_degrade_fan_synthesize(result)
    assert degraded["degraded"] is True
    assert degraded["output"] == "much longer proposal text"


def test_run_workflow_degrades_when_synth_exhausted(tmp_path):
    codex = FakeProvider("codex")
    gemini = FakeProvider("gemini")
    claude = FakeProvider("claude", limit_first_n=99)

    async def fake_run(prompt, *, schema, sandbox="read-only", cwd=None, toolset=None):
        if "Synthesize" in prompt:
            raise Exception("should not reach real provider")
        return f"proposal:{prompt[:20]}"

    codex.run = fake_run  # type: ignore[method-assign]
    gemini.run = fake_run  # type: ignore[method-assign]

    runner = Runner(
        "degrade-test",
        {"codex": codex, "gemini": gemini, "claude": claude},
        {"codex": 2, "gemini": 2, "claude": 1},
        journal_dir=str(tmp_path),
    )

    class SynthExhaust(FakeProvider):
        async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None, model=None):
            if "Synthesize" in prompt:
                from iworkflow.providers import RateLimited
                raise RateLimited("simulated")
            return f"ok:{prompt[:12]}"

    runner.providers = {
        "codex": SynthExhaust("codex"),
        "gemini": SynthExhaust("gemini"),
        "claude": SynthExhaust("claude"),
    }

    result = asyncio.run(run_workflow("what is 2+2?", runner=runner, run_id="t1"))
    assert result.get("degraded") is True
    assert result.get("output") is not None


def test_run_workflow_persists_aggregate_result_for_reconnect(tmp_path):
    runner = Runner(
        "persisted-result",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )
    spec = {
        "steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "a"}],
        "output": "{{steps.a.value}}",
    }

    result = asyncio.run(run_workflow(spec=spec, runner=runner, run_id="persisted-result", journal_dir=str(tmp_path)))
    status, loaded, hint = _workflow_status("persisted-result", str(tmp_path))

    assert result["status"] == "DONE"
    assert status == "done"
    assert hint is None
    assert loaded == result


def test_poll_follows_remembered_journal_dir_after_reconnect(tmp_path, monkeypatch):
    server_root = tmp_path / "server"
    repo = tmp_path / "repo"
    server_root.mkdir()
    repo.mkdir()
    monkeypatch.chdir(server_root)

    runner = Runner(
        "remembered-run",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path / "runner-ledger"),
    )
    spec = {
        "steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "a"}],
        "output": "{{steps.a.value}}",
    }

    result = asyncio.run(run_workflow(
        spec=spec, runner=runner, run_id="remembered-run", cwd=str(repo), journal_dir=".iworkflow",
    ))
    poll = asyncio.run(workflow_poll("remembered-run"))

    assert result["journal_dir"] == str(repo / ".iworkflow")
    assert poll["status"] == "done"
    assert poll["journal_dir"] == str(repo / ".iworkflow")
    assert poll["result"] == result


def test_resolve_catalog_loads_from_root(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["hi"]}}}),
        encoding="utf-8",
    )
    catalog = _resolve_catalog(str(tmp_path), None)
    assert catalog is not None
    assert {s.name for s in catalog.all()} == {"local"}


def test_read_events_since_tracks_line_offset(tmp_path):
    run_dir = tmp_path / "runs" / "job1"
    run_dir.mkdir(parents=True)
    events_path = run_dir / "events.jsonl"
    events_path.write_text(
        "\n".join([
            json.dumps({"event": "route", "label": "a"}),
            "",
            json.dumps({"event": "dispatch", "label": "a"}),
            json.dumps({"event": "done", "label": "a"}),
        ]) + "\n",
        encoding="utf-8",
    )

    first, cursor = _read_events_since("job1", str(tmp_path), after=0, limit=10)
    assert [e["event"] for e in first] == ["route", "dispatch", "done"]
    assert cursor == 4  # blank line counts in the offset

    second, cursor2 = _read_events_since("job1", str(tmp_path), after=cursor, limit=10)
    assert second == []
    assert cursor2 == 4


def test_workflow_stream_returns_incremental_events(tmp_path):
    codex = FakeProvider("codex", delay_s=0.05)
    runner = Runner(
        "stream-test",
        {"codex": codex},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )

    async def _run() -> None:
        started = await workflow_start(
            goal="hello",
            run_id="stream-test",
            runner=runner,
            journal_dir=str(tmp_path),
        )
        assert started["status"] == "started"

        chunk = await workflow_stream(
            "stream-test",
            journal_dir=str(tmp_path),
            after=0,
            block_s=0.0,
        )
        assert chunk["stream"] == "events.jsonl"
        assert isinstance(chunk["events"], list)
        assert "next_after" in chunk

        final = await workflow_stream(
            "stream-test",
            journal_dir=str(tmp_path),
            after=chunk["next_after"],
            block_s=2.0,
        )
        assert final["status"] in {"done", "running", "unknown", "unknown_done"}
        if final["status"] == "done":
            assert final.get("result") is not None

    asyncio.run(_run())


def test_sync_workflow_doc_mentions_start_and_stream():
    assert "iworkflow_workflow_start" in SYNC_WORKFLOW_DOC
    assert "iworkflow_workflow_stream" in SYNC_WORKFLOW_DOC
    assert "DEPRECATED" in SYNC_WORKFLOW_DOC


def test_check_sessions_delegates(monkeypatch):
    monkeypatch.setattr(
        "iworkflow.sessions.probe_sessions",
        lambda providers, timeout_s=12.0: {"ready": ["codex"], "sessions": []},
    )
    out = check_sessions(["codex"])
    assert out["ready"] == ["codex"]


def test_workflow_start_failure_reporting(tmp_path):
    from iworkflow.mcp_server import workflow_poll
    
    # 1. Test not_found state
    poll_result = asyncio.run(workflow_poll("non-existent-run", journal_dir=str(tmp_path)))
    assert poll_result["status"] == "not_found"
    assert "not found" in poll_result.get("hint", "")

    # 2. Test failed_to_start state (directory exists but no events)
    run_dir = tmp_path / "runs" / "failed-start-run"
    run_dir.mkdir(parents=True)
    poll_result = asyncio.run(workflow_poll("failed-start-run", journal_dir=str(tmp_path)))
    assert poll_result["status"] == "failed_to_start"

    # 3. Test background task failure caching in _jobs_history
    codex = FakeProvider("codex")
    runner = Runner("fail-run", {"codex": codex}, {"codex": 1}, journal_dir=str(tmp_path))
    
    async def _run() -> None:
        started = await workflow_start(
            workflow="invalid-recipe-name-xyz",
            run_id="fail-run",
            runner=runner,
            journal_dir=str(tmp_path),
        )
        assert started["run_id"] == "fail-run"
        assert started["status"] == "error"
        assert "invalid-recipe-name-xyz" in str(started.get("error"))

    asyncio.run(_run())


def test_workflow_start_preflight_error_includes_run_id(monkeypatch):
    import iworkflow.mcp_server as mcp_server
    from iworkflow.workflow import WorkflowError

    def fail_preflight(execution, cwd, *, ignore_paths=()):
        raise WorkflowError("pre-flight check failed in /repo: dirty")

    monkeypatch.setattr(mcp_server, "check_preflight", fail_preflight)

    async def _run() -> None:
        started = await workflow_start(
            spec={
                "execution": {"worktree": "new:branch"},
                "steps": [{"id": "a", "kind": "agent", "prompt": "a"}],
            },
            run_id="debug-brainstorm-v3",
        )
        assert started == {
            "run_id": "debug-brainstorm-v3",
            "status": "error",
            "error": "pre-flight check failed in /repo: dirty",
        }

    asyncio.run(_run())


def test_workflow_start_runs_preflight_once(monkeypatch, tmp_path):
    import iworkflow.mcp_server as mcp_server
    import iworkflow.workflow as workflow_mod

    calls = {"n": 0}

    def count_preflight(execution, cwd, *, ignore_paths=()):
        calls["n"] += 1

    monkeypatch.setattr(mcp_server, "check_preflight", count_preflight)
    monkeypatch.setattr(workflow_mod, "check_preflight", count_preflight)

    runner = Runner(
        "preflight-once",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )

    async def _run() -> None:
        started = await workflow_start(
            spec={
                "execution": {"worktree": "new:branch"},
                "steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "a"}],
            },
            run_id="preflight-once",
            runner=runner,
            journal_dir=str(tmp_path),
        )
        assert started["status"] == "started"
        assert started["journal_dir"] == str(tmp_path)
        await asyncio.sleep(0.05)
        assert calls["n"] == 1

    asyncio.run(_run())


def test_workflow_stream_terminal_states(tmp_path):
    import time

    t_start = time.time()
    res = asyncio.run(workflow_stream(
        "non-existent-run",
        journal_dir=str(tmp_path),
        after=0,
        block_s=5.0,
    ))
    duration = time.time() - t_start
    assert res["status"] == "not_found"
    assert duration < 1.0

    (tmp_path / "runs" / "empty-run").mkdir(parents=True)
    t_start = time.time()
    res = asyncio.run(workflow_stream(
        "empty-run",
        journal_dir=str(tmp_path),
        after=0,
        block_s=5.0,
    ))
    duration = time.time() - t_start
    assert res["status"] == "failed_to_start"
    assert duration < 1.0


def test_run_workflow_logs_emit_failure(tmp_path, capsys):
    class BrokenEmitRunner:
        def _emit(self, *args, **kwargs):
            raise OSError("disk full")

    async def _run() -> None:
        with pytest.raises(Exception):
            await run_workflow(
                spec={"steps": [{"id": "missing-kind"}]},
                runner=BrokenEmitRunner(),
                journal_dir=str(tmp_path),
            )

    asyncio.run(_run())
    err = capsys.readouterr().err
    assert "failed to write run error event" in err
    assert "disk full" in err

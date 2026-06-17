import asyncio
import json

from iworkflow.mcp_server import (
    check_sessions,
    SYNC_WORKFLOW_DOC,
    _maybe_degrade_fan_synthesize,
    _read_events_since,
    _resolve_catalog,
    _resolve_run_id,
    run_workflow,
    workflow_start,
    workflow_stream,
)
from iworkflow import FakeProvider, Runner


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

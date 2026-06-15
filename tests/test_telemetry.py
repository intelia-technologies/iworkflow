import asyncio
import json

from iworkflow import FakeProvider, Runner, ToolCatalog, ToolKind, ToolSpec, run_summary


def test_events_log_captures_full_trace_and_run_summary(tmp_path):
    catalog = ToolCatalog()
    catalog.register(ToolSpec("postgres", ToolKind.MCP, "Query the database", tags=("db",)))
    runner = Runner(
        "telem",
        {"codex": FakeProvider("codex", limit_first_n=1), "gemini": FakeProvider("gemini")},
        {"codex": 1, "gemini": 1},
        journal_dir=str(tmp_path),
        catalog=catalog,
    )
    # codex throttles once → fails over to gemini; a tool is injected by tag
    asyncio.run(runner.agent("query the db", label="a", role="doer", tools=["db"]))

    events_path = tmp_path / "runs" / "telem" / "events.jsonl"
    assert events_path.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    kinds = {e["event"] for e in events}
    assert {"route", "dispatch", "limited", "done"} <= kinds   # full trace logged

    route = next(e for e in events if e["event"] == "route")
    assert route["tools"] == ["postgres"]                       # injected tool recorded
    assert route["kind"] == "role=doer"                         # routing decision recorded

    summary = run_summary(journal_dir=str(tmp_path), run_id="telem")
    assert summary["done"] == 1
    assert summary["failovers"] >= 1                            # codex limited → gemini
    assert summary["by_provider"] == {"gemini": 1}
    assert summary["success_rate"] == 1.0


def test_ledger_record_carries_kind_and_tools(tmp_path):
    runner = Runner(
        "telem2",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )
    asyncio.run(runner.agent("implement the thing", label="j", role="doer"))
    ledger = json.loads((tmp_path / "runs" / "telem2" / "ledger.jsonl").read_text().strip())
    assert ledger["kind"] == "role=doer"
    assert ledger["tools"] == []

import asyncio

from iworkflow.mcp_server import (
    _maybe_degrade_fan_synthesize,
    _resolve_run_id,
    run_workflow,
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

    # Force synth label to exhaust by making all providers limit on synth only
    class SynthExhaust(FakeProvider):
        async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None):
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

import asyncio

from iworkflow import AgentResult, FakeProvider, Runner


def test_pipeline_runs_three_items_through_two_fake_provider_stages(tmp_path):
    codex = FakeProvider("codex")
    gemini = FakeProvider("gemini")
    runner = Runner(
        "pipeline-success",
        {"codex": codex, "gemini": gemini},
        {"codex": 2, "gemini": 2},
        journal_dir=str(tmp_path),
    )

    async def stage_one(prev, original, index):
        assert prev == original
        return await runner.agent(
            f"stage one {original}",
            label=f"stage-one-{index}",
            prefer=["codex"],
        )

    async def stage_two(prev, original, index):
        assert isinstance(prev, AgentResult)
        assert prev.ok
        return await runner.agent(
            f"stage two {original}",
            label=f"stage-two-{index}",
            prefer=["gemini"],
        )

    results = asyncio.run(runner.pipeline(["a", "b", "c"], stage_one, stage_two))

    assert len(results) == 3
    assert all(isinstance(result, AgentResult) for result in results)
    assert all(result.ok for result in results)
    assert all(result.provider == "gemini" for result in results)
    assert codex._calls == 3
    assert gemini._calls == 3


def test_pipeline_stage_exception_drops_only_that_item(tmp_path):
    provider = FakeProvider("codex")
    runner = Runner(
        "pipeline-drop-one",
        {"codex": provider},
        {"codex": 2},
        journal_dir=str(tmp_path),
    )

    async def maybe_raise(prev, original, index):
        assert prev == original
        if original == "bad":
            raise RuntimeError("drop this item")
        return await runner.agent(
            f"keep {original}",
            label=f"keep-{index}",
            prefer=["codex"],
        )

    results = asyncio.run(
        runner.pipeline(["good-1", "bad", "good-2"], maybe_raise)
    )

    assert results[1] is None
    assert results[0].ok
    assert results[2].ok
    assert [result.provider if result is not None else None for result in results] == [
        "codex",
        None,
        "codex",
    ]
    assert provider._calls == 2

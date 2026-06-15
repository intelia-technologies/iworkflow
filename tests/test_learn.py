import asyncio

from iworkflow import FakeProvider, Runner, adjust_order


def test_adjust_order_demotes_unreliable_provider():
    stats = {
        "codex": {"done": 1, "rate_limited": 5, "error": 0, "success_rate": 0.167},
        "gemini": {"done": 4, "rate_limited": 0, "error": 0, "success_rate": 1.0},
    }
    assert adjust_order(["codex", "gemini"], stats) == ["gemini", "codex"]


def test_adjust_order_keeps_healthy_and_unknown_in_capability_order():
    stats = {"codex": {"done": 5, "rate_limited": 0, "error": 0, "success_rate": 1.0}}
    # gemini unknown → kept; healthy codex kept; capability order preserved
    assert adjust_order(["codex", "gemini"], stats) == ["codex", "gemini"]


def test_adjust_order_ignores_low_sample_counts():
    stats = {"codex": {"done": 0, "rate_limited": 1, "error": 0, "success_rate": 0.0}}
    assert adjust_order(["codex", "gemini"], stats) == ["codex", "gemini"]


def test_runner_learn_demotes_throttled_provider_from_ledger(tmp_path):
    # seed: codex throttles across a batch, gemini handles it
    seed = Runner(
        "seed",
        {"codex": FakeProvider("codex", limit_first_n=99), "gemini": FakeProvider("gemini")},
        {"codex": 2, "gemini": 2},
        journal_dir=str(tmp_path),
    )
    asyncio.run(seed.parallel([
        lambda i=i: seed.agent(f"implement {i}", label=f"s{i}", role="doer")
        for i in range(4)
    ]))

    # a NEW learning runner reads those ledgers: codex success_rate 0.0 over 4 → demote
    learner = Runner(
        "after",
        {"codex": FakeProvider("codex"), "gemini": FakeProvider("gemini")},
        {"codex": 2, "gemini": 2},
        journal_dir=str(tmp_path),
        learn=True,
    )
    res = asyncio.run(learner.agent("implement y", label="j", role="doer"))

    # default doer prior is [codex, gemini]; learning demotes codex → gemini leads
    assert res.provider == "gemini"
    assert res.attempts[0].provider == "gemini"

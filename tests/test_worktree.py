import asyncio
import subprocess
from pathlib import Path

from iworkflow.worktree import WorktreePool, run_write_agents


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_repo(repo: Path) -> None:
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")


def test_worktree_pool_adds_disjoint_worktrees_and_cleans_dirs(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    with WorktreePool(str(repo)) as pool:
        first = pool.add("first")
        second = pool.add("second")

        (first / "first.txt").write_text("first\n")
        (second / "second.txt").write_text("second\n")

        assert first.exists()
        assert second.exists()
        assert (first / "first.txt").exists()
        assert not (first / "second.txt").exists()
        assert (second / "second.txt").exists()
        assert not (second / "first.txt").exists()
        assert not (repo / "first.txt").exists()
        assert not (repo / "second.txt").exists()
        assert git(repo, "branch", "--list", "iworkflow/first") != ""
        assert git(repo, "branch", "--list", "iworkflow/second") != ""

    assert not first.exists()
    assert not second.exists()
    assert git(repo, "branch", "--list", "iworkflow/first") != ""
    assert git(repo, "branch", "--list", "iworkflow/second") != ""


def test_run_write_agents_dispatches_workspace_write_in_isolated_worktrees(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    class RecordingRunner:
        def __init__(self):
            self.calls = []

        async def agent(self, prompt, *, label, sandbox, cwd, prefer=None):
            worktree = Path(cwd)
            assert worktree.exists()
            (worktree / f"{label}.txt").write_text(prompt)
            self.calls.append(
                {
                    "label": label,
                    "prompt": prompt,
                    "sandbox": sandbox,
                    "cwd": cwd,
                    "prefer": prefer,
                }
            )
            return {"ok": True, "label": label}

        async def parallel(self, thunks):
            return list(await asyncio.gather(*(thunk() for thunk in thunks)))

    runner = RecordingRunner()

    results = asyncio.run(
        run_write_agents(
            runner,
            str(repo),
            [
                {"label": "alpha", "prompt": "alpha body", "prefer": ["codex"]},
                {"label": "beta", "prompt": "beta body"},
            ],
        )
    )

    assert [result["label"] for result in results] == ["alpha", "beta"]
    assert [result["branch"] for result in results] == [
        "iworkflow/alpha",
        "iworkflow/beta",
    ]
    assert [call["sandbox"] for call in runner.calls] == [
        "workspace-write",
        "workspace-write",
    ]
    assert runner.calls[0]["prefer"] == ["codex"]
    assert runner.calls[1]["prefer"] is None
    assert runner.calls[0]["cwd"] != runner.calls[1]["cwd"]
    assert all(not Path(result["worktree"]).exists() for result in results)
    assert not (repo / "alpha.txt").exists()
    assert not (repo / "beta.txt").exists()
    assert git(repo, "branch", "--list", "iworkflow/alpha") != ""
    assert git(repo, "branch", "--list", "iworkflow/beta") != ""

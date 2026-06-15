"""Per-agent git worktree isolation for parallel write workers."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any


class WorktreePool:
    """Create temporary git worktrees and remove their checkout directories."""

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root)
        self._worktrees: list[Path] = []

    def __enter__(self) -> "WorktreePool":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.cleanup()

    def add(self, name: str) -> Path:
        worktree = Path(tempfile.mkdtemp(prefix=f"iworkflow-{name}-"))
        branch = f"iworkflow/{name}"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                str(worktree),
                "-b",
                branch,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self._worktrees.append(worktree)
        return worktree

    def cleanup(self) -> None:
        for worktree in reversed(self._worktrees):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self._worktrees.clear()
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "prune"],
            check=False,
            capture_output=True,
            text=True,
        )


async def run_write_agents(runner: Any, repo_root: str, specs: list[dict]) -> list[dict]:
    with WorktreePool(repo_root) as pool:
        jobs: list[tuple[dict, str, Path]] = []
        for spec in specs:
            label = spec["label"]
            worktree = pool.add(label)
            jobs.append((spec, f"iworkflow/{label}", worktree))

        async def run_one(spec: dict, branch: str, worktree: Path) -> dict:
            label = spec["label"]
            result = await runner.agent(
                spec["prompt"],
                label=label,
                sandbox="workspace-write",
                cwd=str(worktree),
                prefer=spec.get("prefer"),
            )
            return {
                "label": label,
                "branch": branch,
                "worktree": str(worktree),
                "result": result,
            }

        return await runner.parallel(
            [
                lambda spec=spec, branch=branch, worktree=worktree: run_one(
                    spec,
                    branch,
                    worktree,
                )
                for spec, branch, worktree in jobs
            ]
        )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="iworkflow-worktree-self-test-") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "initial")

        with WorktreePool(str(repo)) as pool:
            first = pool.add("selftest-one")
            second = pool.add("selftest-two")

            (first / "one.txt").write_text("one\n")
            _git(first, "add", "-A")
            _git(first, "commit", "-m", "add one")

            (second / "two.txt").write_text("two\n")
            _git(second, "add", "-A")
            _git(second, "commit", "-m", "add two")

            assert (first / "one.txt").exists()
            assert not (first / "two.txt").exists()
            assert (second / "two.txt").exists()
            assert not (second / "one.txt").exists()
            assert not (repo / "one.txt").exists()
            assert not (repo / "two.txt").exists()
            assert _git(repo, "branch", "--list", "iworkflow/selftest-one")
            assert _git(repo, "branch", "--list", "iworkflow/selftest-two")

    print("worktree self-test OK")


if __name__ == "__main__":
    _self_test()

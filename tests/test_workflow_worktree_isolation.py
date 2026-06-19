import asyncio
import subprocess
from pathlib import Path

import pytest

import iworkflow.workflow as workflow
from iworkflow import Limits, Provider, ProviderError, Runner, WorkflowError, run_spec


def _run(coro):
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _write_limits() -> Limits:
    return Limits(
        allow_tools=True,
        allowed_sandboxes=frozenset({"read-only", "write"}),
    )


def _runner(tmp_path: Path, repo: Path, provider: Provider, run_id: str) -> Runner:
    return Runner(
        run_id,
        {"codex": provider},
        {"codex": 4},
        journal_dir=str(tmp_path / "journal"),
        default_cwd=str(repo),
    )


def test_is_git_repo_and_worktree_path_helpers(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    plain = tmp_path / "plain"
    plain.mkdir()

    assert workflow._is_git_repo(repo) is True
    assert workflow._is_git_repo(plain) is False

    first = workflow._worktree_path("run", "fan", "alpha")
    second = workflow._worktree_path("run", "fan", "beta")
    assert first != second
    assert first.name == "iwf-run-fan-alpha"
    assert second.name == "iwf-run-fan-beta"


def test_parallel_write_agents_use_distinct_worktrees_and_consolidate(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    consolidated = []
    original_consolidate = workflow._consolidate_worktree

    async def record_consolidate(base_cwd, wt_path):
        consolidated.append(wt_path)
        await original_consolidate(base_cwd, wt_path)

    monkeypatch.setattr(workflow, "_consolidate_worktree", record_consolidate)

    class ConcurrentWriter(Provider):
        def __init__(self):
            super().__init__("codex")
            self.calls = []
            self.arrived = 0
            self.ready = asyncio.Event()

        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            name = prompt.rsplit(" ", 1)[-1]
            root = Path(cwd)
            (root / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            self.arrived += 1
            if self.arrived == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=2)
            other = "beta" if name == "alpha" else "alpha"
            other_visible = (root / f"{other}.txt").exists()
            base_visible_before_consolidation = (repo / f"{name}.txt").exists()
            self.calls.append({"name": name, "cwd": cwd, "other_visible": other_visible})
            return {
                "name": name,
                "other_visible": other_visible,
                "base_visible_before_consolidation": base_visible_before_consolidation,
            }

    provider = ConcurrentWriter()
    spec = {
        "steps": [{
            "id": "fan",
            "kind": "parallel",
            "agents": [
                {
                    "id": "alpha",
                    "prefer": ["codex"],
                    "sandbox": "write",
                    "tools": ["write"],
                    "write_paths": [str(repo / "alpha.txt")],
                    "prompt": "write alpha",
                },
                {
                    "id": "beta",
                    "prefer": ["codex"],
                    "sandbox": "write",
                    "tools": ["write"],
                    "write_paths": ["beta.txt"],
                    "prompt": "write beta",
                },
            ],
        }]
    }

    out = _run(run_spec(_runner(tmp_path, repo, provider, "wt-parallel"), spec, limits=_write_limits()))

    assert out["status"] == "DONE"
    assert {call["name"] for call in provider.calls} == {"alpha", "beta"}
    cwds = {Path(call["cwd"]) for call in provider.calls}
    assert len(cwds) == 2
    assert repo not in cwds
    assert all(not call["other_visible"] for call in provider.calls)
    assert all(not value["value"]["base_visible_before_consolidation"] for value in out["steps"]["fan"])
    assert (repo / "alpha.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (repo / "beta.txt").read_text(encoding="utf-8") == "beta\n"
    assert len(consolidated) == 2
    assert all(not cwd.exists() for cwd in cwds)


def test_parallel_read_only_agent_uses_default_cwd_without_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    created = []
    original_create = workflow._create_worktree

    async def record_create(base_cwd, wt_path, branch):
        created.append(wt_path)
        await original_create(base_cwd, wt_path, branch)

    monkeypatch.setattr(workflow, "_create_worktree", record_create)

    class MixedProvider(Provider):
        def __init__(self):
            super().__init__("codex")
            self.calls = []

        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            self.calls.append({"prompt": prompt, "sandbox": sandbox, "cwd": cwd})
            if sandbox != "read-only":
                Path(cwd, "allowed.txt").write_text("ok\n", encoding="utf-8")
            return {"prompt": prompt}

    provider = MixedProvider()
    spec = {
        "steps": [{
            "id": "fan",
            "kind": "parallel",
            "agents": [
                {"id": "audit", "prefer": ["codex"], "prompt": "read only"},
                {
                    "id": "write",
                    "prefer": ["codex"],
                    "sandbox": "write",
                    "tools": ["write"],
                    "write_paths": ["allowed.txt"],
                    "prompt": "write allowed",
                },
            ],
        }]
    }

    out = _run(run_spec(_runner(tmp_path, repo, provider, "wt-read-only"), spec, limits=_write_limits()))

    assert out["status"] == "DONE"
    assert len(created) == 1
    by_prompt = {call["prompt"]: call for call in provider.calls}
    assert by_prompt["read only"]["cwd"] == str(repo)
    assert by_prompt["write allowed"]["cwd"] != str(repo)
    assert not created[0].exists()


def test_parallel_write_outside_write_paths_fails_without_consolidating(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    class WrongWriter(Provider):
        def __init__(self):
            super().__init__("codex")
            self.cwd = None

        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            self.cwd = Path(cwd)
            Path(cwd, "outside.txt").write_text("bad\n", encoding="utf-8")
            return {"wrote": "outside"}

    provider = WrongWriter()
    spec = {
        "steps": [{
            "id": "fan",
            "kind": "parallel",
            "agents": [{
                "id": "bad",
                "prefer": ["codex"],
                "sandbox": "write",
                "tools": ["write"],
                "write_paths": ["allowed.txt"],
                "prompt": "write outside",
            }],
        }]
    }

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(_runner(tmp_path, repo, provider, "wt-disallowed"), spec, limits=_write_limits()))

    message = str(exc_info.value)
    assert "agent step 'fan:bad' wrote outside allowed paths" in message
    assert "outside.txt" in message
    assert "allowed.txt" in message
    assert not (repo / "outside.txt").exists()
    assert provider.cwd is not None
    assert not provider.cwd.exists()


def test_parallel_write_failure_removes_worktree_and_propagates_required_failure(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    class FailingWriter(Provider):
        def __init__(self):
            super().__init__("codex")
            self.cwd = None

        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            self.cwd = Path(cwd)
            Path(cwd, "allowed.txt").write_text("partial\n", encoding="utf-8")
            raise ProviderError("simulated provider failure")

    provider = FailingWriter()
    spec = {
        "steps": [{
            "id": "fan",
            "kind": "parallel",
            "agents": [{
                "id": "bad",
                "prefer": ["codex"],
                "sandbox": "write",
                "tools": ["write"],
                "write_paths": ["allowed.txt"],
                "prompt": "write then fail",
            }],
        }]
    }

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(_runner(tmp_path, repo, provider, "wt-failure"), spec, limits=_write_limits()))

    assert "agent step 'fan:bad' exhausted" in str(exc_info.value)
    assert not (repo / "allowed.txt").exists()
    assert provider.cwd is not None
    assert not provider.cwd.exists()


def test_parallel_write_in_non_git_directory_fails_before_provider_call(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    class RecordingProvider(Provider):
        def __init__(self):
            super().__init__("codex")
            self.calls = 0

        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            self.calls += 1
            return {"ok": True}

    provider = RecordingProvider()
    spec = {
        "steps": [{
            "id": "fan",
            "kind": "parallel",
            "agents": [{
                "id": "writer",
                "prefer": ["codex"],
                "sandbox": "write",
                "tools": ["write"],
                "write_paths": ["allowed.txt"],
                "prompt": "write",
            }],
        }]
    }
    runner = Runner(
        "wt-non-git",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path / "journal"),
        default_cwd=str(plain),
    )

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner, spec, limits=_write_limits()))

    message = str(exc_info.value)
    assert "worktree isolation required for write-capable agents in parallel/pipeline steps" in message
    assert str(plain) in message
    assert "not a git repository" in message
    assert provider.calls == 0


def test_pipeline_write_stage_consolidates_before_next_stage(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    class PipelineWriter(Provider):
        def __init__(self):
            super().__init__("codex")
            self.stage2_saw_stage1 = False
            self.cwd_by_prompt = {}

        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            root = Path(cwd)
            self.cwd_by_prompt[prompt] = root
            if prompt == "stage one":
                (root / "stage1.txt").write_text("one\n", encoding="utf-8")
                return {"stage": "one"}
            self.stage2_saw_stage1 = (root / "stage1.txt").exists()
            (root / "stage2.txt").write_text("two\n", encoding="utf-8")
            return {"stage": "two", "saw_stage1": self.stage2_saw_stage1}

    provider = PipelineWriter()
    spec = {
        "steps": [{
            "id": "pipe",
            "kind": "pipeline",
            "items": ["item"],
            "stages": [
                {
                    "id": "one",
                    "prefer": ["codex"],
                    "sandbox": "write",
                    "tools": ["write"],
                    "write_paths": ["stage1.txt"],
                    "prompt": "stage one",
                },
                {
                    "id": "two",
                    "prefer": ["codex"],
                    "sandbox": "write",
                    "tools": ["write"],
                    "write_paths": ["stage2.txt"],
                    "prompt": "stage two",
                },
            ],
        }]
    }

    out = _run(run_spec(_runner(tmp_path, repo, provider, "wt-pipeline"), spec, limits=_write_limits()))

    assert out["status"] == "DONE"
    assert provider.stage2_saw_stage1 is True
    assert out["steps"]["pipe"][0]["value"]["saw_stage1"] is True
    assert (repo / "stage1.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo / "stage2.txt").read_text(encoding="utf-8") == "two\n"
    assert all(not path.exists() for path in provider.cwd_by_prompt.values())

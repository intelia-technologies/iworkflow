#!/usr/bin/env python3
"""Orchestrate roadmap #4 (worktree-per-agent write isolation) WITH iworkflow.

  implement → Codex (workspace-write, single) builds iworkflow/worktree.py +
              a `cwd` passthrough so write-agents run in their own git worktree
  audit     → Gemini (read-only) reviews isolation correctness

Parent verifies: the new self-test + the FULL existing suite must stay green.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import CodexProvider, GeminiProvider, Runner, log  # noqa: E402

SPEC = r"""Implement roadmap item: per-agent git WORKTREE isolation for parallel
WRITE workers, so two agents writing code at once never clobber a shared checkout.
Keep ALL existing tests green (`.venv/bin/python -m pytest -q` is 31 passing now).

Two parts:

(A) A small `cwd` passthrough so an agent can run in a specific directory:
  - iworkflow/providers.py: `Provider.run(..., cwd: str | None = None)` and thread
    `cwd` into the subprocess. `_exec(argv, stdin, cwd=None)` -> pass cwd to
    asyncio.create_subprocess_exec(..., cwd=cwd). CodexProvider/GeminiProvider use
    it directly; ClaudeInteractiveProvider should `cd {cwd} && claude ...` in its
    tmux session command when cwd is given. FakeProvider.run must accept cwd too
    (ignore it) so existing tests keep passing.
  - iworkflow/scheduler.py: Runner.agent(..., cwd: str | None = None) forwards cwd
    to prov.run(...). Default None = unchanged behaviour.

(B) A NEW file iworkflow/worktree.py (stdlib + subprocess only):
  - class WorktreePool(repo_root: str): a context manager that creates and cleans
    up git worktrees.
      .add(name) -> Path : runs `git -C <repo> worktree add <tmp> -b iworkflow/<name>`
        into a fresh temp dir, tracks it, returns the worktree Path.
      .cleanup() : `git worktree remove --force` each + best-effort prune.
      __enter__/__exit__ -> cleanup on exit.
  - async def run_write_agents(runner, repo_root, specs) -> list[dict]:
      specs is a list of dicts {label, prompt, prefer?(list)}. For each spec it
      adds a worktree, calls runner.agent(prompt, label=label,
      sandbox="workspace-write", cwd=str(worktree), prefer=spec.get("prefer")),
      and returns [{label, branch, worktree, result}]. Run them with
      runner.parallel([...]) (safe now — disjoint worktrees). Leave the per-agent
      branches in place for review; remove the worktree dirs at the end.
  - A `if __name__ == "__main__":` self-test using tempfile: init a temp git repo
    (git init, configure user, one commit), open a WorktreePool, add two worktrees,
    write a DIFFERENT file in each + `git add -A && git commit` inside each worktree,
    assert: each worktree contains only its own new file, the main checkout is
    unchanged, and the two branches exist. Cleanup. Print 'worktree self-test OK'.

Use `git -C <dir>` for all git calls. Type hints (X | None, list[...], dict[...]).
Run `python iworkflow/worktree.py` and `.venv/bin/python -m pytest -q` yourself
and confirm both pass before reporting."""

REPORT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "files_touched", "summary"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "files_touched": {"type": "array", "items": {"type": "string"}},
                   "summary": {"type": "string"}},
}


async def main():
    runner = Runner(
        run_id="build-worktrees-1",
        providers={"codex": CodexProvider("codex", timeout_s=480),
                   "gemini": GeminiProvider("gemini", timeout_s=220)},
        caps={"codex": 1, "gemini": 1})

    log("PHASE implement (codex, workspace-write)")
    impl = await runner.agent(
        SPEC, label="implement:worktrees", schema=REPORT,
        prefer=["codex"], sandbox="workspace-write")
    print(f"\nIMPLEMENT [{impl.provider}] → {impl.value}")

    log("PHASE audit (gemini, read-only)")
    audit = await runner.agent(
        "Read iworkflow/worktree.py and the cwd changes in iworkflow/providers.py + "
        "scheduler.py. Is the worktree isolation real (each agent truly on its own "
        "branch+dir, no shared-index clobber)? Any leak, cleanup bug, or cwd that "
        "isn't actually applied? 6 sentences max.",
        label="audit:worktrees", prefer=["gemini", "codex"])
    print(f"\nAUDIT [{audit.provider}] →\n{audit.value}")
    return {"implement": impl.value, "audit": audit.value}


if __name__ == "__main__":
    asyncio.run(main())

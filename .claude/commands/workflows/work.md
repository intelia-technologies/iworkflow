---
description: Execute an iworkflow plan with tests, focused commits, and continuous verification
argument-hint: "[plan path or clear task]"
allowed-tools: Read, Glob, Grep, Write, Edit, Bash(git:*), Bash(ls:*), Bash(find:*), Bash(rg:*), Bash(python3:*), Bash(.venv/bin/python:*)
---

# Work

Execute an iworkflow implementation plan.

## Required context

Input: $ARGUMENTS

Before editing code, read:

- `AGENTS.md`
- `README.md`
- The plan path, if provided
- Relevant source files and tests

If the task is code behavior, write or update a focused test first and watch it fail before implementation. For docs or prompt-only changes, add the smallest deterministic validation that proves discovery, structure, or references.

## Git safety

- Do not overwrite user changes.
- Do not stash, reset, or discard unrelated changes.
- Do not push directly to `main`.
- Commit only if the user asked for commits or the workflow explicitly reaches land/ship.

## Implementation loop

For each plan checkbox:

1. Implement only the scoped behavior.
2. Keep `iworkflow/` stdlib-only unless the plan explicitly puts dependencies behind extras.
3. Use `FakeProvider` or local fixtures for tests; do not spend CLI quota in ordinary tests.
4. Run the targeted test or validation for the changed behavior.
5. Mark plan checkboxes only after the behavior is implemented and verified.
6. Keep docs/examples aligned when behavior changes.

## Completion gate

Before handing off, run the plan's verification commands. At minimum for non-trivial changes:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 examples/demo_fakes.py
```

Run `python3 examples/workflow_dynamic.py` when declarative workflow behavior changed.

## Output

Report:

- Files changed
- Tests and commands run with results
- Any skipped verification and why
- Remaining risks
- Recommended next command: `/workflows:review` or `/workflows:land`

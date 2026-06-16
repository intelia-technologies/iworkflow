---
description: Create an iworkflow implementation plan from a brainstorm or requirements
argument-hint: "[brainstorm path, recipe/spec path, issue, or requirements]"
allowed-tools: Read, Glob, Grep, Write, Edit, Bash(git:*), Bash(ls:*), Bash(find:*), Bash(rg:*)
---

# Plan

Create a concrete implementation plan for iworkflow.

## Input resolution

Input: $ARGUMENTS

If the input is:

- A brainstorm path under `docs/design/`, read it first.
- A workflow recipe/spec path, inspect the spec and the interpreter paths it exercises.
- Raw requirements, gather enough context to write the plan directly.
- Empty, look for recent brainstorms in `docs/design/` and ask only if ambiguous.

## Context to gather

Read the relevant parts of:

- `AGENTS.md` and `README.md`
- `iworkflow/*.py` touched by the change
- `tests/test_*.py` covering those modules
- `examples/demo_fakes.py`, `examples/workflow_dynamic.py`, or other relevant examples
- Existing `docs/design/*.md` notes for related decisions

## Plan artifact

Write:

```text
docs/design/YYYY-MM-DD-<topic>-plan.md
```

The plan must include:

- Goal and non-goals
- Current behavior
- Proposed behavior
- Files likely touched
- Data/control-flow changes
- Compatibility and safety concerns
- Runtime dependency impact, explicitly stating whether `iworkflow/` remains stdlib-only
- Subscription impact, explicitly stating whether any live CLI calls are required
- Implementation phases with checkboxes
- Verification table mapping each behavior to a deterministic command

## Verification expectations

Prefer subscription-free verification:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 examples/demo_fakes.py
```

Add targeted commands when relevant:

```bash
.venv/bin/python -m pytest tests/test_workflow.py -q
python3 examples/workflow_dynamic.py
```

Live smoke commands such as `python3 examples/demo_live.py codex gemini` are optional and require explicit user approval.

## Handoff

End with:

- Plan path
- Open questions
- Risk level
- Recommended next command: `/workflows:work <plan-path>`

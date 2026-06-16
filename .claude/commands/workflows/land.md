---
description: Validate an iworkflow branch, prepare PR evidence, and handle final review safely
argument-hint: "[PR number, branch, plan path, or empty for current branch]"
allowed-tools: Read, Glob, Grep, Edit, Bash(git:*), Bash(gh:*), Bash(ls:*), Bash(find:*), Bash(rg:*), Bash(python3:*), Bash(.venv/bin/python:*)
---

# Land

Validate and prepare an iworkflow change for PR, review, or merge.

## Input resolution

Input: $ARGUMENTS

- If input is a PR number, inspect that PR.
- If input is a plan path, use it as intent.
- If input is empty, infer from the current branch and working tree. Ask only if ambiguous.

## Validation gate

Run and fix until clean, unless the user explicitly asks for review-only:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 examples/demo_fakes.py
```

Run when relevant:

```bash
python3 examples/workflow_dynamic.py
```

Do not run live adapter demos unless explicitly approved:

```bash
python3 examples/demo_live.py codex gemini
```

## PR evidence

Prepare a concise PR summary with:

- What changed
- Why it fits iworkflow's hard rules
- Tests and demos run
- Any skipped live checks and why
- Risks or follow-up work

## Review and merge safety

- Stop on unresolved human changes requested.
- Do not admin-merge.
- Do not push or merge unless the user explicitly asked for that action.
- If closing issues, verify each issue is fully solved before using closing keywords.

## Output

Report:

- Current branch and PR URL, if any
- Validation evidence
- Review status
- Merge readiness and remaining blockers

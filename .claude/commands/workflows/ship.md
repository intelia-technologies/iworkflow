---
description: Run the full iworkflow development pipeline from idea to validated PR-ready change
argument-hint: "[idea, brainstorm path, plan path, or branch]"
allowed-tools: Read, Glob, Grep, Write, Edit, Bash(git:*), Bash(gh:*), Bash(ls:*), Bash(find:*), Bash(rg:*), Bash(python3:*), Bash(.venv/bin/python:*)
---

# Ship

Run the full local iworkflow pipeline.

## Pipeline

```text
/workflows:brainstorm
  -> docs/design/*-brainstorm.md
/workflows:plan
  -> docs/design/*-plan.md
/workflows:work
  -> code/docs/tests
/workflows:review
  -> findings or all-clear
/workflows:land
  -> validation evidence and PR-ready state
```

## Direct path

For tiny docs edits, typo fixes, or test-only cleanups, say that the full pipeline is unnecessary and use the direct path:

1. Inspect context.
2. Make the small change.
3. Run targeted verification.
4. Report evidence.

## Rules

- Do not use live CLI smoke tests unless explicitly approved.
- Do not merge, deploy, or push without explicit user instruction.
- Do not skip review or verification for code behavior changes.
- Do not let implementation drift from the plan; update the plan when scope changes.

## Output

Report:

- Artifact paths created
- Files changed
- Verification commands and results
- Review outcome
- Whether the branch is PR-ready

---
description: Review iworkflow changes for intent, tests, deterministic behavior, and subscription safety
argument-hint: "[plan path, PR number, branch, or file list]"
allowed-tools: Read, Glob, Grep, Bash(git:*), Bash(gh:*), Bash(ls:*), Bash(find:*), Bash(rg:*)
---

# Review

Run a high-signal iworkflow code review. Findings lead. Every finding must be concrete and actionable.

## Determine scope

Input: $ARGUMENTS

If no input is provided:

```bash
gh pr view --json number,url,body 2>/dev/null
git log main..HEAD --oneline
git diff main..HEAD --stat
git status --short
```

If there is no PR or branch diff, review the current working tree diff.

## Gather intent

Use this fallback chain:

1. Provided plan/brainstorm under `docs/design/`
2. PR body
3. Commit messages
4. Current conversation context

Summarize intent before reviewing.

## Review lenses

Check only issues that matter for this repo:

- **Subscription-only:** no provider API calls, no API-key SDK dependency, no default `claude -p` worker path.
- **Determinism:** orchestration control flow remains plain Python/spec execution; models do not decide runner control flow.
- **Stdlib core:** no runtime dependency added to `iworkflow/`; optional integrations stay behind extras.
- **Provider asymmetry:** Codex for structured doers, Gemini for schema-less audit/sweeps, Claude reserved for interactive driver/delicate paths.
- **Safety limits:** untrusted workflow specs stay bounded by `Limits`; no sandbox/tool injection widening without explicit trusted caller.
- **Tests:** behavior changes have subscription-free tests using `FakeProvider` or local fixtures.
- **Verification:** evidence includes the targeted command and, when relevant, `demo_fakes` or `workflow_dynamic`.

## Output

Use this structure:

```markdown
## Findings

- [severity] file:line - Issue. Why it matters. Exact fix.

## Open Questions

- Question or assumption, if any.

## Verification Gaps

- Missing command/evidence, if any.

## Summary

Brief change summary only after findings.
```

If no issues are found, say that clearly and still list residual verification risk.

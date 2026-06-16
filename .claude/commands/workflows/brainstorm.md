---
description: Explore what to build or change in iworkflow before planning implementation
argument-hint: "[idea, problem, recipe name, or workflow spec path]"
allowed-tools: Read, Glob, Grep, Bash(git:*), Bash(ls:*), Bash(find:*), Bash(rg:*)
---

# Brainstorm

Explore WHAT to build before deciding HOW to build it.

## Input

Topic: $ARGUMENTS

If no topic is provided, ask what idea, bug, recipe, or workflow behavior should be explored.

## Ground rules

- Preserve the hard rules in `AGENTS.md`: subscription-only workers, deterministic orchestration, stdlib-only core, and provider asymmetry.
- Do not call paid provider APIs or add API-key SDK dependencies.
- Do not run live adapter demos unless the user explicitly asks.
- Prefer small, deterministic changes that can be proven with `FakeProvider` tests.

## Workflow

1. Read `AGENTS.md`, `README.md`, and any relevant files under `docs/design/`.
2. Search the codebase for related modules, examples, recipes, tests, and prior design notes.
3. Summarize the current state and the concrete pain point.
4. Ask focused clarifying questions only when the answer cannot be inferred safely.
5. Propose 2-3 approaches with trade-offs. Include at least one conservative option.
6. Recommend one approach and explain why it best fits iworkflow's constraints.

## Durable output

For non-trivial implementation-bound work, write:

```text
docs/design/YYYY-MM-DD-<topic>-brainstorm.md
```

Include:

- Problem statement
- Current behavior and relevant files
- Explored approaches
- Recommended direction
- Explicit non-goals
- Verification strategy, using subscription-free tests first
- Open questions

End with the next command:

```text
/workflows:plan docs/design/YYYY-MM-DD-<topic>-brainstorm.md
```

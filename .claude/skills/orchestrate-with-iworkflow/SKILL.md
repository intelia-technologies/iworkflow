---
name: orchestrate-with-iworkflow
description: Use when authoring or running iworkflow recipes, dynamic workflow specs, multi-agent fan-out, gated agent pipelines, or subscription-CLI orchestration in this repo
---

# Orchestrate With iworkflow

Use iworkflow when the task is a deterministic program over agents: gates,
fan-out, pipelines, loops, result threading, resume, provider failover, or
toolset injection. Do not use it for a one-off interactive design dialogue or a
single local edit.

## Core Rules

- Keep orchestration deterministic. Python/spec execution decides control flow;
  models only return data.
- Keep workers subscription-only. Use CLI adapters (`codex exec`, `agy -p`,
  interactive Claude via tmux) and never provider API SDKs in the core path.
- Keep `iworkflow/` stdlib-only. Optional integrations stay behind extras.
- Route by provider asymmetry: Codex for structured doers, Gemini for
  schema-less audit/sweeps, Claude for interactive driver or delicate cores.
- Prove behavior with `FakeProvider` tests before any live adapter smoke.

## When To Use

Use iworkflow for:

- A homogeneous batch of independent review/build/sweep tasks.
- A gate that must pass before spending fan-out work.
- A pipeline where each item needs staged agent work.
- A bounded loop with a critic, vote, dry-run, count, or budget stop condition.
- A reusable workflow that should calcify into `.iworkflow/recipes/*.json`.
- A worker prompt that benefits from selected project tools/skills/commands.

Stay in the main agent loop for:

- Human-led brainstorming.
- One-file edits.
- Unclear scope that needs conversation before decomposition.
- Work that cannot be bounded by `Limits`.

## Workflow Shape

Scout first. Identify the work list, risk, current files, tests, examples, and
the exact verification command. The topology comes from that scouting, not from
a generic template.

Choose the primitive by write-contention:

| Need | Primitive |
| --- | --- |
| One foundation must pass first | `agent` with `gate.abort_on` |
| Disjoint independent work | `parallel` |
| Per-item staged flow | `pipeline` |
| Completeness search under a hard cap | `loop` |
| Reusable confirmed flow | named recipe in `.iworkflow/recipes/` |

Prefer gate -> fan -> reconcile for risky work. A single integrator should see
all upstream returns and run the mandatory verification.

## Spec Pattern

Use declarative specs across MCP or untrusted boundaries. Do not pass arbitrary
Python closures from agents.

```json
{
  "steps": [
    {
      "id": "contract",
      "kind": "agent",
      "prompt": "Define the contract for {{params.topic}}.",
      "schema": {
        "type": "object",
        "required": ["verdict", "summary"],
        "properties": {
          "verdict": {"type": "string", "enum": ["DONE", "BLOCKED"]},
          "summary": {"type": "string"}
        }
      },
      "gate": {"abort_on": {"verdict": "BLOCKED"}}
    },
    {
      "id": "audit",
      "kind": "agent",
      "prefer": ["gemini", "codex"],
      "prompt": "Audit the result independently: {{steps.contract.value.summary}}"
    }
  ]
}
```

Doers should return structured tri-state verdicts when control flow depends on
their output. Independent auditors can stay schema-less so they do not collapse
nuance into a shallow enum.

## Token Economy

iworkflow buys isolation, traceability, gates, and resume — NOT token savings.
Every worker is a stateless CLI one-shot: whatever you inline into a prompt is
re-ingested on every dispatch. A recipe that hands the same corpus to N agents
costs N full passes (a real 4 MB diagnosis run measured 3-6x a monolithic run).

Rules that keep the overhead bounded:

- **Pass evidence by REFERENCE, not by value.** Workers are CLIs with
  filesystem access — write the corpus to files once and prompt with paths
  plus a scope ("read only sections X/Y"). Never template megabytes into
  `{{...}}`. The runner warns (`corpus_reread` event) when the same >100k-char
  prompt is dispatched twice.
- **Partition, don't replicate.** Fan-out adjudicators should each read a
  disjoint slice; together they equal ONE pass, not N.
- **One synthesizer, fed summaries.** The reconciler reads upstream structured
  returns, never the raw corpus again.
- **Deterministic gates over LLM verifiers.** Coverage, uniqueness, ID
  resolution, JSON-pointer checks, manifest safety — that's `kind: command`
  steps (free), not another agent pass. Reserve at most ONE targeted auditor,
  scoped to flagged items and their evidence_refs only.
- **Budget the run.** `Runner(token_budget=..., budget_action="warn"|"abort")`
  or `IWORKFLOW_TOKEN_BUDGET=2000000[:abort]` caps total spend; gemini/cursor
  usage is estimated (~4 chars/token, flagged `estimated`) so the rollup and
  the `TOKENS run total` teardown line reflect ALL providers, not just
  codex/claude.
- **Use iworkflow where mutations are** (manifest-driven repair: load only
  evidence_refs, re-check state, dry-run, human gate, single mutation,
  verify) — and a single strong model + command validators for read-only
  diagnosis.

## Limits And Safety

For untrusted specs, keep default `Limits` conservative:

- Read-only sandbox.
- Tool injection off.
- Bounded total agent calls.
- Bounded parallel width, pipeline items, loop depth, and loop iterations.
- No sandbox widening unless the caller is trusted and passes explicit limits.

If a workflow needs writes, prefer worktree isolation and explicit paths. Never
parallelize agents that write the same file or mutable sandbox.

## Verification

Use subscription-free checks first:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 examples/demo_fakes.py
python3 examples/workflow_dynamic.py
```

Run live adapters only with explicit approval:

```bash
python3 examples/demo_live.py codex gemini
```

After a run, treat agent returns as untrusted notifications. Re-read the
journal, inspect outputs, rerun the relevant tests, and report auditor caveats
honestly.

## Common Mistakes

- Using iworkflow before scouting the real work list.
- Running `parallel` over shared files or one mutable environment.
- Trusting a worker's self-report without rerunning verification.
- Adding provider SDK dependencies because they are easier than CLI adapters.
- Letting untrusted specs widen sandbox, tools, or loop/fan-out limits.
- Turning a useful dynamic spec into a recipe before it has passed real tests.

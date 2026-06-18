# Using iworkflow — agent reference

A reference for an **agent** (Claude / Codex / Gemini) that wants to *drive* iworkflow:
what it can do and exactly how to invoke it. For the design rationale read
[`README.md`](../README.md); for contributor rules read [`AGENTS.md`](../AGENTS.md).

## What iworkflow is (in one paragraph)

A **deterministic, subscription-only, rate-limit-aware orchestrator** of multi-agent
workflows. The orchestration is plain Python (zero LLM/coordination tokens); the
*workers* are subscription-authenticated coding-agent CLIs (`codex exec`, `agy -p`,
interactive `claude` via tmux). You describe a workflow as **data** (a JSON spec) or
pick a **named recipe**; iworkflow fans the work across the CLIs with per-provider
concurrency caps, fails over to another subscription when one is throttled, journals
every step, and can resume across rate-limit windows. It never calls a metered
provider API.

**When to use it:** a deterministic program over agents — gates, fan-out, pipelines,
loops, result-threading, adversarial verification, broad sweeps, migrations.
**When NOT to:** a one-off interactive design chat or a single local edit.

## The two doors

| Door | How | Use when |
|---|---|---|
| **Named recipe** | `workflow="review"` + `params` | a proven shape exists for the task |
| **Dynamic spec** | `spec={...}` (JSON, define-your-own) | you need a custom shape; safe over MCP (data, not code) |

A dynamic spec that proves useful can be saved as a named recipe (`.iworkflow/recipes/*.json`)
— the *dynamic → confirmed → preset* calcification.

## How to invoke — three doors, same engine

> **It is NOT "humans use the CLI, agents use MCP".** The CLI, the MCP tool, and the
> Python SDK all call the *same* `run_workflow` — the choice is **how you reach the
> engine**, not what it can do. **An agent can drive iworkflow over either the CLI**
> (shell out with Bash) **or MCP** (a registered tool). The worker behaviour is
> identical regardless of the door.

### A) CLI — a person, CI/automation, or an agent via Bash
The widest surface (full toolbelt: `run`, `workflows`, `stats`, `catalog`, `register`).
```bash
iworkflow workflows                       # list recipes + their params
iworkflow run review --params '{"topic":"the scheduler","subject_a":"…","subject_b":"…"}'
iworkflow run --spec my_spec.json --params '{"q":"…"}' --run-id job1
iworkflow stats --run-id job1             # telemetry from past runs
iworkflow catalog                         # tools/skills discovered in the repo
iworkflow register [--codex|--claude]     # wire the MCP server into this repo
iworkflow graph review                     # write an HTML diagram to a temp file (+ open it) and print the path
iworkflow graph review --mermaid           # print raw Mermaid to stdout (for piping/redirect)
iworkflow graph review --html graph.html   # write the HTML diagram to a specific path
iworkflow graph review --publish           # publish HTML diagram to shareable URL via nosdrop
```
An agent reaches this by running the commands through its shell/Bash tool; it must
**capture and parse stdout** (the bundle JSON; logs go to stderr).

### B) MCP — an agent as a registered tool
The narrowest, smoothest surface for an agent that should compose iworkflow into its
own reasoning. `iworkflow register` once, then these tools appear:

- `iworkflow_ping()` — liveness.
- `iworkflow_list_workflows(recipe_dir?)` — recipes (built-in + host dir).
- `iworkflow_workflow_start(goal? | workflow?+params? | spec?, …)` — **start** a
  workflow without blocking. Returns `{run_id, status: "started"}` or a
  synchronous `{run_id, status: "error", error: "pre-flight check failed: ..."}`.
- `iworkflow_workflow_stream(run_id, after=0, block_s=5)` — **incremental progress**
  from `events.jsonl` (SSE-like long-poll). Use returned `next_after` as cursor.
- `iworkflow_workflow_poll(run_id)` — snapshot poll (status + tail of events).
- `iworkflow_workflow(…)` — **sync / blocking** run. **Deprecated for long runs**
  (>~30s MCP timeout). Prefer `start` + `stream`/`poll`.

Shared optional params on start/sync tools:
- `cwd` — working directory for provider CLIs (workers see this repo).
- `caps` — per-provider concurrency, e.g. `{"codex": 2, "gemini": 2}`.
- `catalog_root` — load MCP/skills/commands from a repo (same discovery as CLI
  `iworkflow catalog`).
- `recipe_dir` — host recipes under `.iworkflow/recipes` or a custom path.
- `journal_dir` — where `.iworkflow/runs/<run_id>/` is written (default `.iworkflow`).

Pass **exactly one** driver to start/sync:
  - `spec={...}` — your own dynamic workflow (the DYNAMIC door).
  - `workflow="<name>"` + `params={...}` — a predefined recipe.
  - `goal="<question>"` — sugar for `fan_synthesize` over one question.

**Long-run pattern (recommended):**
```
start = iworkflow_workflow_start(goal="…", cwd="/path/to/repo")
after = 0
while True:
  chunk = iworkflow_workflow_stream(start["run_id"], after=after, block_s=5)
  after = chunk["next_after"]
  # handle chunk["events"] …
  if chunk["status"] in {"done", "error", "unknown_done", "failed_to_start", "not_found"}:
    break
```

Terminal stream statuses are `done`, `error`, `unknown_done`, `failed_to_start`,
and `not_found`. `failed_to_start` usually means the run directory was created but
no event could be written; `not_found` means no run directory exists for that
`run_id` in the selected `journal_dir`.

The result returns as a **structured tool result** (no stdout parsing), and tools
are **discoverable** in the agent's tool list.

#### MCP stdio lifecycle / disconnection

Claude Code owns stdio MCP server processes. If `iworkflow-mcp` is killed while a
Claude Code session is active, that session's tool registry can mark the
`iworkflow_*` tools as unavailable and will not reconnect them mid-session. A new
`iworkflow-mcp` PID elsewhere does not repair the broken stdio pipe. Recovery is
client-side: restart the Claude Code session (or remove/re-add the MCP server if
the client supports that) so tools are rediscovered.

Do not use `pkill -f iworkflow-mcp` as a normal refresh path for a live session. If
MCP tools are unavailable, use the CLI door (`iworkflow run`, `iworkflow status`,
`iworkflow stats`) from Bash against the same `.iworkflow` journal until the host
session is restarted.

### C) SDK — embedding, tests, custom providers, widening Limits
```python
from iworkflow import Runner, run_spec, Limits, FakeProvider
# inject your own Runner (e.g. FakeProvider for 0-quota tests), or widen the policy:
await run_spec(runner, spec, params, limits=Limits(allow_tools=True))
```
The **only** door that can widen `Limits` (privileged sandbox, tool injection) or swap
providers. This is what the test suite and `examples/` use.

### Which door (for an agent)
- **MCP** when it's registered → structured I/O, discoverable, `start`+`stream` for
  long runs. The default for "this agent orchestrates iworkflow inline" while the
  MCP stdio connection is healthy.
- **CLI (Bash)** when you need `stats`, `graph`, explicit `--run-id` control, or
  there's no MCP host / the MCP stdio process died mid-session. The universal
  fallback.
- **SDK** when embedding iworkflow in code, writing tests, or you must raise `Limits`.
- They compose: launch over MCP, inspect the same run with `iworkflow stats` over Bash
  — the journal is shared.

The result of a run is a **bundle**:
```jsonc
{ "status": "DONE" | "ABORTED",
  "name": "<recipe or null>",
  "output": <rendered spec.output>,
  "steps": { "<step_id>": <that step's value>, … },
  "aborted_at": "<step_id>"   // only when ABORTED
}
```

## Built-in recipes

| Recipe | Shape | Params |
|---|---|---|
| `fan_synthesize` | two proposers (different angles) → one synthesis | `goal` |
| `review` | gate → fan two disjoint reviews → independent adversarial audit | `topic`, `subject_a`, `subject_b` |
| `roadmap` | each provider proposes from its strength → one prioritizes | `context` |
| `deep_review` | loop: find new issues until a critic says complete (capped) | `subject` |
| `adaptive_review` | gate → fan reviews → **supervisor injects a deep audit only on ISSUES** | `topic`, `subject_a`, `subject_b` |

## Writing a dynamic spec

A spec is `{ name?, description?, params?, schemas?, output?, steps:[…] }`. Each step
has an `id`, a `kind`, optional `needs` (prior step ids), and kind-specific fields.

### Step kinds

| kind | what it does | key fields |
|---|---|---|
| `agent` | one worker call | `prompt`, `schema?`, `prefer?`, `role?`, `gate?`, `sandbox?`, `tools?` |
| `parallel` | fan-out **barrier** of agents | `agents:[…]` |
| `pipeline` | per-item staged flow, **no barrier** between stages | `items` (→list), `stages:[…]` |
| `loop` | repeat a `body` until a stop condition | `body:[…]`, `until`, `max_iterations` (required), `collect?` |
| `supervisor` | a coordinator inspects state and **adapts the remaining plan** | `prompt`, `watch?`, `when?` |

### Templating
Prompts (and most string fields) render against the run context:
- `{{params.*}}` — inputs.
- `{{steps.<id>.value.*}}` — a prior step's structured value.
- inside a `loop`: `{{loop.collected}}`, `{{loop.iteration}}`, `{{loop.decision.*}}`.
- inside a `pipeline` stage: `{{item}}`, `{{prev}}`, `{{index}}`.
- inside a `supervisor`: `{{supervisor.steps}}`, `{{supervisor.remaining}}`, `{{supervisor.params}}`.

A string that is **exactly** one `{{token}}` resolves to the raw object (so
`"items": "{{loop.collected}}"` stays a list); otherwise tokens stringify inline.

### `agent` — schema, routing, gate
```jsonc
{ "id": "gate", "kind": "agent", "schema": "gate",
  "prefer": ["claude", "codex"],          // provider preference (else routed by role/kind)
  "gate": { "field": "verdict", "abort_on": "BLOCKED" },  // abort the whole run on this value
  "prompt": "Is {{params.topic}} sound? verdict=DONE to proceed, BLOCKED if broken." }
```
- `schema` is a name (registered in `schemas`, or built-ins `decision`/`supervision`)
  or an inline JSON-Schema dict. Codex/Claude enforce it natively; Gemini parses a
  JSON block.
- `prefer` overrides routing; omit it to let routing pick by `role`/inferred task kind
  (and, with `learn=True`, demote providers the ledger shows failing).

### `parallel` — fan-out barrier
```jsonc
{ "id": "fan", "kind": "parallel", "needs": ["gate"], "agents": [
    { "id": "a", "schema": "review", "prefer": ["codex","gemini"], "prompt": "Review A: {{params.subject_a}}" },
    { "id": "b", "schema": "review", "prefer": ["gemini","codex"], "prompt": "Review B: {{params.subject_b}}" } ] }
```
`steps.fan.value` is a list of `{id, value, provider, ok}`.

### `pipeline` — per-item staged flow (no barrier)
Item A can be in stage 3 while item B is still in stage 1.
```jsonc
{ "id": "p", "kind": "pipeline", "items": "{{params.files}}",
  "stages": [
    { "id": "review", "schema": "findings", "prompt": "Review {{item}}." },
    { "id": "verify", "prompt": "Adversarially verify: {{prev.value}}" } ] }
```

### `loop` — repeat until a stop condition (always `max_iterations`-capped)
```jsonc
{ "id": "sweep", "kind": "loop", "max_iterations": 4,
  "until": { "agent": { "prompt": "Found {{loop.collected}}. Complete? verdict=STOP if done.",
                        "stop_when": "STOP", "prefer": ["gemini","claude"] } },
  "collect": { "from": "find", "path": "findings", "dedup_by": "title" },
  "body": [ { "id": "find", "kind": "agent", "schema": "findings",
              "prompt": "Find NEW issues not in {{loop.collected}}; prioritise {{loop.decision.missing}}." } ] }
```
`until` is exactly one of:

| `until` | stops when |
|---|---|
| `{"times": N}` | N iterations have run |
| `{"count": {"target": N}}` | the accumulator reaches N items (needs `collect`) |
| `{"dry": {"rounds": K}}` | K consecutive rounds add nothing new (needs `collect`) |
| `{"budget": {"output_tokens": N}}` | N output tokens spent in the loop |
| `{"agent": {"prompt", "stop_when", …}}` | a **critic agent** returns the stop verdict (its `missing` feeds the next round) |
| `{"vote": {"prompt", "count", "threshold", "lenses"?}}` | a **majority of N judges** votes stop (optional perspective `lenses`) |

### `supervisor` — adaptive re-planning (the coordinator)
Inspects accumulated state at a checkpoint and emits a **decision as data** the
deterministic executor applies to the *remaining* plan. **Top-level only.**
```jsonc
{ "id": "supervise", "kind": "supervisor", "needs": ["fan"], "watch": ["fan"],
  "prefer": ["claude","codex"],
  "when": { "any": [   // OPTIONAL guard: fire the coordinator ONLY on deviation
    { "path": "steps.fan.value", "select": "value.verdict", "in": ["ISSUES"] } ] },
  "prompt": "Reviews: {{supervisor.steps}}. If a review is ISSUES, action=adjust and inject one audit; else action=continue." }
```
The coordinator returns the built-in `supervision` schema:
```jsonc
{ "action": "continue | adjust | abort",
  "reason": "…",
  "skip":   ["future_step_id"],          // drop future steps
  "set_params": { "k": v },              // overlay params (future templating)
  "inject": [ { /* a step spec */ } ] }  // new steps, re-parsed under the SAME Limits
```
- Only the **tail** is mutable (the past is immutable).
- Injected steps **cannot escalate** sandbox/tools (same validator); a malformed
  inject degrades gracefully (dropped).
- Bounded by `Limits.max_supervisions`; journaled, so a **resume re-applies the same
  mutation with zero new provider calls**.
- The `when` guard makes the common, on-track path spend **zero coordinator tokens**.
  Leaves: `{"path":"<dotted>", <op>: operand}` with ops `eq` `ne` `in` `nin` `gte`
  `lte` `gt` `lt` `contains` `truthy` `exists`, an optional `select` sub-path applied
  per list element; combine with `all`/`any`/`not`.

## Provider asymmetry (who does what)

| Provider (CLI) | Best at | Structured output |
|---|---|---|
| **Codex** (`codex exec`) | structured doers / codegen | native `--output-schema` |
| **Gemini** (`agy -p`) | schema-less adversarial audit, 1M-token sweeps | parses a JSON block |
| **Claude** (interactive, tmux) | driver / delicate high-value cores (scarce: cap 1) | pane extraction |

Default routing already encodes this; use `prefer`/`role` to steer, or trust the
router. Never rely on `claude -p` (metered) as a worker.

## Safety / Limits (a spec can arrive from an untrusted agent over MCP)

Defaults (widen explicitly only from a trusted CLI/SDK caller via `Limits(...)`):

| bound | default |
|---|---|
| sandbox allowlist | `{"read-only"}` (a spec can't request a privileged sandbox) |
| tool injection | off |
| `max_total_agent_calls` | 64 |
| `max_parallel_width` | 16 |
| `max_pipeline_items` | 256 |
| `max_loop_depth` | 3 |
| `max_loop_iterations` | 100 |
| `max_supervisions` | 8 |

## Telemetry & resume (free, a side effect of running)

Per run under `.iworkflow/runs/<run_id>/`:
- `events.jsonl` — full structured trace (`route`/`dispatch`/`done`/`limited`/`error`/
  `cooling`/`exhausted`/`resumed`).
- `ledger.jsonl` — durable per-agent record (also drives resume): provider, attempts,
  prompt/schema hashes, timing, `kind`, `tools`, input/output tokens, cost.
- `wf-steps.json` — completed top-level steps (step-boundary resume).

Re-running the **same `run_id`** short-circuits completed steps (zero new provider
calls). Inspect with `iworkflow stats [--run-id …]`, or in code
`iworkflow.provider_stats(...)` / `iworkflow.run_summary(...)`.

## Recipes for common goals (pick a shape)

- **Decide between options** → `fan_synthesize` (or a `parallel` proposers → `agent` synth).
- **Review a design/diff** → `review` (gate → fan → independent audit).
- **Exhaustive bug/issue hunt** → `deep_review` (agent-decided loop) or a `loop` with
  `until:{dry}` / `until:{vote}` + adversarial verify.
- **Adapt to what you find** → `adaptive_review`, or any spec with a `supervisor` step.
- **Process a batch (files/items)** → `pipeline` (review → verify per item, no barrier).
- **Prioritize next work** → `roadmap`.

## Verify your changes without spending quota
```bash
python3 examples/demo_fakes.py        # deterministic proof (cap/failover/resume)
python3 -m pytest -q                  # the suite uses FakeProvider/ScriptedProvider
```

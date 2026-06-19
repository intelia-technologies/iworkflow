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
  workflow without blocking. Returns `{run_id, status: "started", journal_dir}` or a
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
  Relative paths resolve against `cwd` when `cwd` is provided, not against the MCP
  server process directory. `workflow_start` returns the resolved value. The MCP
  server also records a lightweight `.iworkflow/run-index.json` pointer so a later
  `poll`/`stream` from the same server working directory can find the resolved
  journal after reconnect; passing the returned `journal_dir` is still the most
  explicit cross-process path. Preflight ignores the workflow journal path because
  it is iworkflow's own resume state; unrelated dirty files still block worktree
  recipes, and the error lists the blocking dirty paths.

Pass **exactly one** driver to start/sync:
  - `spec={...}` — your own dynamic workflow (the DYNAMIC door).
  - `workflow="<name>"` + `params={...}` — a predefined recipe.
  - `goal="<question>"` — sugar for `fan_synthesize` over one question.

**Long-run pattern (recommended):**
```
start = iworkflow_workflow_start(goal="…", cwd="/path/to/repo")
after = 0
while True:
  chunk = iworkflow_workflow_stream(
      start["run_id"], journal_dir=start["journal_dir"], after=after, block_s=5,
  )
  after = chunk["next_after"]
  # handle chunk["events"] …
  if chunk["status"] in {"done", "paused", "aborted", "error", "unknown_done", "failed_to_start", "not_found"}:
    break
```

Terminal stream statuses are `done`, `paused`, `aborted`, `error`, `unknown_done`,
`failed_to_start`, and `not_found`. Finished aggregate bundles are persisted to
`result.json`, so a reconnected MCP process can return `done` or `paused` with
`result` from disk. `paused` means a `checkpoint` is waiting for human input and
`pending_input` tells the operator what to review/write. `unknown_done` means only
per-agent events are present and no aggregate `result.json` exists.
`failed_to_start` usually means the run directory was created but no event could
be written; `not_found` means no run directory exists for that `run_id` in the
selected or remembered `journal_dir`.

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
{ "status": "DONE" | "PAUSED" | "ABORTED",
  "name": "<recipe or null>",
  "output": <rendered spec.output>,
  "steps": { "<step_id>": <that step's value>, … },
  "pending_input": { "step_id": "gate", "prompt": "…", "output": "/path/decision.json" }, // only when PAUSED
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

A spec is `{ name?, description?, params?, schemas?, output?, artifacts?, steps:[…] }`.
Each step has an `id`, a `kind`, optional `needs` (prior step ids), optional `when`,
and kind-specific fields. `artifacts` lists required output files/dirs validated before `DONE`; relative
artifact paths resolve against workflow `cwd`.

### Step kinds

| kind | what it does | key fields |
|---|---|---|
| `agent` | one worker call | `prompt`, `schema?`, `prefer?`, `role?`, `gate?`, `sandbox?`, `tools?`, `write_paths?`, `required?`, `when?` |
| `parallel` | fan-out **barrier** of agents | `agents:[…]`, `when?` |
| `pipeline` | per-item staged flow, **no barrier** between stages | `items` (→list), `stages:[…]`, `when?` |
| `loop` | repeat a `body` until a stop condition | `body:[…]`, `until`, `max_iterations` (required), `collect?`, `when?` |
| `supervisor` | a coordinator inspects state and **adapts the remaining plan** | `prompt`, `watch?`, `when?` |
| `command` | one local subprocess | `command`, `cwd?`, `env?`, `timeout_s?`, `gate?`, `when?` |
| `checkpoint` | human-in-the-loop gate; pauses until a JSON resolution exists or an interactive resolver answers | `prompt`, `output`, `mode?`, `schema?`, `artifact?`, `when?` |

### Templating
Prompts (and most string fields) render against the run context:
- `{{params.*}}` — inputs.
- `{{steps.<id>.value.*}}` — a prior step's structured value.
- inside a `loop`: `{{loop.collected}}`, `{{loop.iteration}}`, `{{loop.decision.*}}`.
- inside a `pipeline` stage: `{{item}}`, `{{prev}}`, `{{index}}`.
- inside a `supervisor`: `{{supervisor.steps}}`, `{{supervisor.remaining}}`, `{{supervisor.params}}`.

A string that is **exactly** one `{{token}}` resolves to the raw object (so
`"items": "{{loop.collected}}"` stays a list); otherwise tokens stringify inline.

### Conditional Routing — `when`
Any top-level `agent`, `parallel`, `pipeline`, `loop`, `command`, or `checkpoint` step can include
`when`. The runner evaluates it deterministically against the accumulated context
before dispatching the step. If the predicate is false, no provider, CLI, or
subprocess runs; the step is journaled as `{"skipped": true, "ok": true, "kind": ...}`,
emits a `skipped` event, and satisfies downstream `needs`. Dependents do not inherit
the skip automatically; add their own `when` if they should branch too.

Available paths include `params.*`, `steps.<id>.value.*`, and metadata on a skipped
step such as `steps.<id>.skipped` or `steps.<id>.kind`. A leaf predicate is
`{"path": "<dotted>", <op>: operand}` with operators `eq`, `ne`, `in`, `nin`,
`gte`, `lte`, `gt`, `lt`, `contains`, `truthy`, and `exists`. Use `select` to apply
a sub-path to each element when the path resolves to a list; combine predicates with
`all`, `any`, and `not`.

```jsonc
{
  "steps": [
    { "id": "tests", "kind": "command", "command": "pytest -q" },
    { "id": "audit", "kind": "agent", "needs": ["tests"],
      "when": { "path": "steps.tests.value.exit_code", "eq": 0 },
      "prefer": ["codex"],
      "prompt": "Audit the passing test run: {{steps.tests.value.stdout}}" },
    { "id": "notify", "kind": "agent", "needs": ["audit"],
      "when": { "path": "steps.audit.skipped", "truthy": true },
      "prompt": "Explain that audit was skipped because tests failed." }
  ]
}
```

### `agent` — schema, routing, gate
```jsonc
{ "id": "gate", "kind": "agent", "schema": "gate",
  "prefer": ["claude", "codex"],          // provider preference (else routed by role/kind)
  "gate": { "field": "verdict", "abort_on": "BLOCKED" },  // abort the whole run on this value
  "prompt": "Is {{params.topic}} sound? verdict=DONE to proceed, BLOCKED if broken." }
```
- `schema` is a name (registered in `schemas`, or built-ins `decision`/`supervision`)
  or an inline JSON-Schema dict. Codex/Claude enforce it natively; Gemini parses a
  JSON block. When `schema` is set, iworkflow also validates the complete returned
  `res.value` in the engine before marking the agent `DONE`; a mismatch emits
  `schema_mismatch` with `label`, `provider`, and `why`, then tries the next
  provider. If every provider mismatches, `required:true` fails the sequential step
  and `required:false` continues with `value:null` / `ok:false`. Parallel and
  pipeline subagents validate independently, so one bad subresult marks that element
  and the aggregate step `ok:false`.
- `prefer` overrides routing; omit it to let routing pick by `role`/inferred task kind
  (and, with `learn=True`, demote providers the ledger shows failing).
- `write_paths` should be set on any `sandbox:"write"` / `tools:["write"]` step.
  After the agent returns, iworkflow compares Git dirty paths and fails the step if
  the agent touched anything outside the declared repo-relative paths.
- `required` defaults to `true`; if all preferred providers fail/exhaust, a sequential
  agent fails the workflow instead of feeding `null` into downstream prompts. Use
  `required:false` only for explicit best-effort/degraded steps.

### `checkpoint` — human gate / PAUSED resume
```jsonc
{ "id": "draft_gate", "kind": "checkpoint", "needs": ["render"],
  "mode": "input",
  "prompt": "Review render/email.html. Approve, edit variables, or abort.",
  "artifact": "{{steps.render.value.email_html}}",
  "schema": { "type": "object", "required": ["approved"],
              "properties": { "approved": { "type": "boolean" }, "notes": {} } },
  "output": ".iworkflow/runs/{{params.run_id}}/draft_decision.json" }
```
- Unattended mode (default): if `output` does not exist or does not validate, the
  run returns `status:"PAUSED"`, persists `result.json`, emits `checkpoint_pending`,
  and does **not** execute dependent steps. Write/fix the JSON file and relaunch the
  same `run_id`; completed prior steps are journaled and the workflow resumes at the
  checkpoint.
- Attended mode: `iworkflow run --interactive …` installs a terminal resolver. The
  resolver returns the human decision inline; iworkflow validates it, writes `output`,
  and continues in the same process. SDK callers can pass `Runner(...,
  checkpoint_resolver=callable)`.
- `mode:"input"` requires `schema`; the resolution must validate before advancing.
  Invalid JSON/schema mismatches keep the run paused with `pending_input.validation_error`.
- `mode:"confirm"` advances only on an explicit affirmative value (`{"approved": true}`
  or `"go"`/`"yes"`). Ambiguous or negative answers are not a go.

This maps supervised workflows such as `review-client-v4`: Gate 1 writes
`decisions.json` before task/document writes, Gate 2 approves or edits
`render/variables.json` before draft creation, and Gate 3 requires an explicit send
confirmation before `email.send_draft`.

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

### Write isolation with git worktrees
Write-capable agents inside `parallel` and `pipeline` steps run in isolated git
worktrees automatically. An agent is write-capable when `sandbox` is not
`"read-only"` or its `tools` includes `"write"`. Top-level `agent` steps keep the
existing cwd behavior.

Requirements:
- `cwd` must be inside a git repository and git must support `worktree`
  (git 2.5+).
- Each write-capable subagent should declare `write_paths`; paths are checked
  inside that agent's worktree.

Behavior:
- The worktree path is deterministic under the system temp directory, keyed by
  `run_id`, step id, and agent id.
- Read-only subagents in the same `parallel` or `pipeline` step still run in the
  runner default cwd.
- Successful writes are squashed back into the base working tree before the parent
  step returns. Validation or provider failures skip consolidation.
- Worktrees are removed with `git worktree remove --force` on success and failure.
- If a write-capable `parallel` or `pipeline` subagent runs outside git, the
  workflow fails before calling any provider.

```jsonc
{
  "steps": [{
    "id": "edit_docs",
    "kind": "parallel",
    "agents": [
      {
        "id": "usage",
        "prefer": ["codex"],
        "sandbox": "write",
        "tools": ["write"],
        "write_paths": ["docs/USING_IWORKFLOW.md"],
        "prompt": "Update the usage guide."
      },
      {
        "id": "audit",
        "prefer": ["gemini"],
        "prompt": "Review the docs for consistency."
      }
    ]
  }]
}
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

### Claude tmux isolation

Interactive Claude workers run in a tmux server dedicated to the iworkflow run:
`tmux -L iw_<run_id> ...`. The individual Claude sessions still use internal
`iwf-*` names, but they live in that per-run socket rather than the user's default
tmux namespace, so `tmux ls` in a normal shell will not show or collide with them.

When the workflow finishes, iworkflow calls `tmux -L iw_<run_id> kill-server`.
That removes the run's tmux server after the provider has already cleaned up its
individual sessions. If no interactive Claude worker was configured, teardown is
a no-op.

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
  `cooling`/`schema_mismatch`/`exhausted`/`resumed`/`checkpoint_pending`).
- `ledger.jsonl` — durable per-agent record (also drives resume): provider, attempts,
  prompt/schema hashes, `schema_ok`, timing, `kind`, `tools`, input/output tokens,
  cost.
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

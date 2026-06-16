# iworkflow

[![CI](https://github.com/intelia-technologies/iworkflow/actions/workflows/ci.yml/badge.svg)](https://github.com/intelia-technologies/iworkflow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

**Dynamic multi-agent workflows over the coding-agent CLIs you already pay for — on your *subscription*, not metered API tokens.**

`iworkflow` is a small, deterministic orchestration runtime. You write a *program
over agents* — gates, fan-out, pipelines, result-threading, structured verdicts —
and the runner executes it, spawning **subscription-authenticated CLIs** (`codex
exec`, `agy -p`, the interactive `claude` TUI) as the workers. Orchestration is
plain Python: **zero coordination tokens**. Workers are the CLIs: **zero API keys**.

It is a provider-agnostic port of Claude Code's dynamic Workflow tool ("ultracode")
that any of those CLIs can also *drive* (via an MCP `workflow` tool), with the one
thing none of the existing orchestrators ship: a **rate-limit-aware scheduler that
spreads work across several subscriptions and survives throttling**.

## Why it exists

The 2026 reality is that the big coding-agent CLIs are cheapest on a flat
**subscription**, but their headless/SDK paths are increasingly **metered at API
rates** (Anthropic split `claude -p` / the Agent SDK out of the subscription pool
on 2026-06-15). So an orchestrator that calls provider *APIs* — LangGraph,
mcp-agent, smolagents — burns money you didn't intend to spend. `iworkflow` only
ever drives the *CLIs*, so all model spend stays inside your existing plans.

The catch is that subscriptions have **rate limits**, and fanning many workers out
trips them. `iworkflow`'s scheduler is built around that:

- **Per-provider concurrency caps** — a semaphore per subscription, tuned to each
  plan, so you never exceed what a plan tolerates.
- **Failover across subscriptions** — when one plan is throttled, the agent
  re-dispatches to another (you usually have Codex + Gemini + Claude).
- **Durable resume** — completed agents are journaled; a run throttled at 80%
  resumes from where it stopped once the limit window resets, instead of dying.
- **Backoff on limit detection** — the worker output is parsed for
  "session/usage/rate limit"; those re-queue instead of failing the run.

## Provider matrix (as of 2026-06)

| Provider | CLI | Subscription? | Structured output | Best role |
|---|---|---|---|---|
| **Codex** | `codex exec` (ChatGPT login) | ✅ included | ✅ native `--output-schema` | structured *doers* |
| **Gemini** | `agy -p` (Antigravity/Google) | ✅ included | ✗ (parse a JSON block) | adversarial *auditor*, 1M-context *sweeps* |
| **Claude** | `claude -p` (headless) | ❌ Pool 2 / API-rate | ✅ native `--json-schema` | avoid as worker (metered) |
| **Claude** | interactive TUI via tmux/pty | ✅ Pool 1 | ✗ (scrape the pane) | delicate *cores*, *driver* |

The provider asymmetry mirrors the orchestration roles: structured doers go to the
CLI with a native schema (Codex), the schema-less adversarial auditor goes to the
schema-less CLI (Gemini), and the scarce/expensive Claude is reserved for the
interactive driver and delicate cores.

## Status

Proven so far (47 tests: `.venv/bin/python -m pytest -q`; `python3 examples/demo_fakes.py`, 5/5):

- **Scheduler** — per-provider concurrency cap, cross-subscription failover,
  throttle-aware cooldown, durable **run ledger** resume, `parallel()` + `pipeline()`.
- **3 subscription workers, live** — `codex exec --output-schema`, `agy -p`, and
  **interactive Claude via tmux (Pool 1)** round-trip real structured output.
- **Routing** — capability-aware defaults (`demo_routing.py`) + **empirical** demotion
  of providers the ledger shows failing (`Runner(learn=True)`).
- **MCP face** — `codex exec` drove the engine via the `iworkflow_*` MCP tools (the
  original goal: `examples/codex_drives_iworkflow.sh`); same server works from agy/Claude.
- **Worktree isolation** — each WRITE worker on its own git worktree+branch
  (`worktree.py`, `agent(cwd=)`), so parallel writers can't clobber.
- **Dynamic toolsets** — inject only the tools/skills a task needs per `agent()`:
  explicit `tools=[...]` → tags → `auto_tools=k` (keyword auto-select). A **portable**
  loader (`load_project_catalog(root)`) discovers any project's catalog
  (`.mcp.json`/`.claude/skills`/`.codex`); measured cost of injecting tool schemas
  (`examples/measure_toolsets.py`).
- **Self-built** — the ledger, the test suite, worktree isolation, and the toolset
  system were each built BY iworkflow orchestrating Codex (write) + Gemini (audit).

Roadmap: tool-RAG (embedding tool-retrieval at catalog scale) · always-on core
toolset + missing-tool retry · tmux backend hardening for long prose · progress TUI.

## Workflows: recipes + dynamic specs

The engine runs *workflows*, not just a single hard-coded shape. Two doors, same
interpreter — mirroring the harness's own `Workflow({name})` vs `Workflow({script})`:

**Predefined recipes** — pick one by name (built-in or host-registered):

```bash
iworkflow workflows                                   # list recipes + params
iworkflow run review --params '{"topic":"the scheduler","subject_a":"...","subject_b":"..."}'
```

Built-ins: `fan_synthesize`, `review` (gate→fan→audit), `roadmap`, `deep_review`
(an agent-decided loop). A host project drops its own `*.json` specs into
`.iworkflow/recipes/` and they appear alongside the built-ins — iworkflow stays
domain-agnostic.

**Dynamic specs** — *define your own* workflow as **data** (safe to pass across MCP,
where a Python closure can't go). A spec is a list of `steps`, each one of:

| kind | runs |
|---|---|
| `agent` | one worker call (optional `schema`, `prefer`, `gate` to abort) |
| `parallel` | a fan-out barrier of agents |
| `pipeline` | per-item staged flow (no barrier between stages) |
| `loop` | repeat a `body` until a stop condition — see below |

Prompts template against `{{params.*}}`, `{{steps.<id>.value.*}}`, and inside a
loop `{{loop.collected}}` / `{{loop.iteration}}` / `{{loop.decision.*}}`.

**Loops** carry a mandatory `max_iterations` cap (anti-runaway) plus one `until`:

| `until` | stops when |
|---|---|
| `{"times": N}` | N iterations have run |
| `{"count": {"target": N}}` | the accumulator reaches N items |
| `{"dry": {"rounds": K}}` | K consecutive rounds add nothing new |
| `{"budget": {"output_tokens": N}}` | N output tokens spent in the loop |
| `{"agent": {...}}` | a **critic agent** returns `STOP` (its `missing` feeds the next round) |
| `{"vote": {...}}` | a **majority of N judge agents** votes `STOP` (optional perspective `lenses`) |

The agent-decided loop is the "completeness critic": the loop runs until a judge
is satisfied, not until a counter trips — the critic *is* the condition, always
under the hard cap. `examples/workflow_dynamic.py` runs a gate→agent-decided-loop
spec deterministically (no quota). An agent drives the same thing over MCP:
`iworkflow_workflow(spec={...})`. A spec that proves itself can be saved as a named
recipe — the dynamic → confirmed → preset calcification.

**Safety policy.** Because a spec can arrive from an untrusted agent over MCP, every
run is bounded by a `Limits` policy (conservative by default): only a `read-only`
sandbox is allowed (a spec can't request `codex exec --sandbox danger-…`), tool
injection is off, and the run is capped on total agent calls, parallel width,
pipeline items, loop nesting depth, and per-loop iterations — so no spec, however
malformed or hostile, can fan out into a fork-bomb of paid CLI spawns. Trusted
callers (CLI/SDK) widen the policy explicitly via `run_spec(..., limits=Limits(...))`.

**Durable resume.** Every completed top-level step (a whole loop included) is journaled
under `.iworkflow/runs/<run_id>/`, so re-running with the same `run_id` short-circuits
finished steps to their stored result — a workflow throttled at 80% picks up where it
stopped instead of re-spending. An in-flight loop resumes mid-flight from its body's
agent-label cache in the scheduler ledger.

## Design notes

- **Deterministic, not model-driven.** Control flow is real code; the model
  doesn't re-decide the orchestration each turn. (Same idea as bernstein's
  "zero coordination tokens".)
- **Pluggable invocation backend** per provider — *headless* where it's still
  subscription-covered, *interactive-terminal (tmux/pty)* as the Pool-1 path and
  as insurance if other vendors copy Anthropic's pool split.
- **Structured-output, gracefully degrading** — native schema where the CLI has
  it, a `{...}`/`STATUS:`/`VERDICT:` marker fallback where it doesn't.

## License

MIT. See `LICENSE`.

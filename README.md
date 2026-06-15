# iworkflow

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

Proven so far:

- Deterministic scheduler — per-provider cap, cross-subscription failover, durable
  ledger resume, `parallel()` + `pipeline()` (`.venv/bin/python -m pytest -q`, 24
  tests; `python3 examples/demo_fakes.py`, 5/5).
- Live adapters — `codex exec --output-schema`, `agy -p`, and **interactive Claude
  via tmux (Pool 1)** round-trip real structured output on the subscription.
- Capability-aware default routing (`python3 examples/demo_routing.py`).
- Durable run ledger (`iworkflow/ledger.py`) and the test suite were **built by
  iworkflow orchestrating itself** (`examples/build_ledger.py`, `build_tests.py`).
- **MCP face works**: `codex exec` drove the engine via the `iworkflow_*` MCP tools
  — the original goal (`bash examples/codex_drives_iworkflow.sh`). The same server
  (`python -m iworkflow.mcp_server`) is reachable from agy and Claude too.

Roadmap: throttle-aware resume loop on the ledger · worktree-per-agent isolation
for write workers · tmux backend hardening for long prose · empirical routing
(learn best provider per task-kind from the ledger) · progress TUI.

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

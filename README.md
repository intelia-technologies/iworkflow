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

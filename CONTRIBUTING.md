# Contributing to iworkflow

Thanks for your interest. iworkflow is a small, deterministic orchestration
runtime — contributions that keep it small and sharp are very welcome.

## Dev setup

```bash
uv venv
uv pip install -e ".[dev,mcp]"     # pytest + ruff (+ the optional MCP face)
uv run pytest -q                    # the suite is subscription-free (FakeProvider)
uv run ruff check .
```

The 50-test suite spawns **no** real CLIs and spends **no** quota — it runs
entirely on `FakeProvider`. Live behaviour is exercised by the opt-in
`examples/` scripts (those do call your subscription CLIs).

## Design principles (please preserve)

1. **Subscription-only.** Workers are the CLIs (`codex exec`, `agy -p`,
   interactive `claude` via tmux). Never call a paid provider API or add an
   API-key SDK dependency to the core.
2. **Deterministic core.** Orchestration is plain Python with zero LLM calls —
   the model never re-decides control flow.
3. **Stdlib-only core.** `iworkflow/` has zero runtime dependencies. Optional
   integrations (the MCP face) go behind extras, never in the core path.
4. **End-state over transitional.** No backward-compat shims unless asked.

See `iworkflow/AGENTS.md` and `docs/design/` for the deeper rationale.

## What to work on

The roadmap in `README.md` lists open items (tool-RAG, an always-on core
toolset + missing-tool retry, tmux hardening for long prose, a progress TUI).
Bug reports with a failing `FakeProvider` test are the most useful.

## PRs

- Keep `uv run pytest -q` and `uv run ruff check .` green.
- Add a test (FakeProvider-based — no quota) for any behaviour change.
- One focused change per PR; match the surrounding style.

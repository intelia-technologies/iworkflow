# iworkflow — agent guide

A deterministic orchestration runtime that spawns **subscription-authenticated
coding-agent CLIs** as workers. Read `README.md` first for the why.

## Hard rules

1. **Subscription-only.** Workers are the CLIs (`codex exec`, `agy -p`,
   interactive `claude` via tmux). NEVER call a paid provider API or add an
   API-key SDK dependency. `claude -p` is metered (Pool 2) — do not use it as a
   default worker.
2. **Deterministic core.** Orchestration is plain Python with zero LLM calls.
   The model never re-decides control flow; the runner executes the program.
3. **Stdlib-only core.** `iworkflow/` keeps zero runtime dependencies. Optional
   integrations (Temporal, MCP server) go behind extras, never in the core path.
4. **Provider asymmetry is intentional.** Codex = structured doers (native
   `--output-schema`); Gemini = schema-less auditor/sweeps; Claude = interactive
   driver / delicate cores. See the matrix in `README.md`.

## Layout

- `iworkflow/scheduler.py` — the runner: per-provider semaphore, failover,
  journal/resume, `agent()` / `parallel()` primitives, role→provider routing.
- `iworkflow/providers.py` — CLI adapters + `FakeProvider` (for deterministic
  tests) + rate-limit detection.
- `iworkflow/minijsonschema.py` — dependency-free schema check (prod: `jsonschema`).
- `examples/demo_fakes.py` — deterministic proof (cap / failover / resume), no quota.
- `examples/demo_live.py` — one real call per CLI to smoke the adapters.

## Verify

```bash
python3 examples/demo_fakes.py     # must print 3/3 scenarios PASS
python3 examples/demo_live.py codex gemini   # live adapters (spends a little)
```

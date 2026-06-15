# Design: telemetry & agent crystallization

Status: telemetry shipped (v0.2); crystallization is a proposal.

The aim: log the whole execution so it can be exploited later — to improve
routing/processes, and eventually to **crystallize specialized agents** (curated
toolset + provider for a use case) from accumulated experience, instead of
assembling every agent on the fly.

## What's logged now (shipped)

Per run, under `<journal_dir>/runs/<run_id>/`:

- **`events.jsonl`** — the full structured execution trace. One JSON event per
  step, each with `ts`, `run_id`, `label`:
  - `route` — `kind` (the routing decision: `role=…` / `inferred=…` / `explicit`
    / `…→learned`), `order` (provider preference), `tools` (the injected toolset).
  - `dispatch` / `done` (`provider`, `ms`) / `limited` / `error` / `cooling` /
    `exhausted` / `resumed`.
- **`ledger.jsonl`** — the durable per-agent record (also drives resume): status,
  provider, `attempts[]` (provider+outcome), `prompt_sha`, `schema_sha`, timing,
  `error_class`, and now **`kind`** + **`tools`**.

Nothing here spends tokens — it's a side effect of running.

## Exploiting it (shipped)

- `iworkflow.stats.provider_stats(journal_dir)` — per-provider DONE/RATE_LIMITED/
  ERROR + success rate + avg latency across all runs.
- `iworkflow.stats.run_summary(journal_dir, run_id)` — a run's agents, providers,
  kinds, failovers, success rate, duration.
- `iworkflow stats [--run-id …]` — the CLI view of both (the logs, explored).
- `iworkflow.learn` already closes the loop: `Runner(learn=True)` demotes providers
  the ledger shows failing. **This is the first, narrow form of "learning from the
  logs."**

## Telemetry fields

- **Tokens + cost per agent — shipped (v0.2).** Providers surface usage as a
  race-free `last_usage` side-channel read right after each call: Codex parses
  its `--json` `turn.completed.usage` (input/output tokens); Claude reads the
  `-p --output-format json` envelope `.usage` + `.total_cost_usd`; Gemini/tmux
  report none (best-effort → `None`). Captured into the `done` event + the ledger
  record (`input_tokens`/`output_tokens`/`cost_usd`); `run_summary` sums them.
- **Next — task text (truncated) + outcome quality.** For deep analysis you want
  more than `prompt_sha`. Opt-in (privacy/size), e.g. first N chars + a verdict.

## Aspirational: agent crystallization

The logs above give, per agent, the tuple:

```
(task kind, tools injected, provider)  →  (success, latency, tokens)
```

That is exactly the training data to **crystallize a preset agent**: for a
recurring use case, the (kind, toolset, provider) that reliably wins.

```python
@dataclass(frozen=True)
class AgentPreset:
    name: str                 # e.g. "reconciliation-debugger"
    role: str | None          # routing kind it serves
    tools: tuple[str, ...]    # the curated toolset (catalog names)
    prefer: tuple[str, ...]   # provider order proven best for it
    # provenance: derived from N runs, success_rate, avg_ms, last_seen

def crystallize(journal_dir: str, min_runs: int = 5,
                min_success: float = 0.9) -> list[AgentPreset]:
    """Mine the ledger/events for stable (kind, tools)→provider clusters with
    enough samples and high success, and emit reusable presets."""
```

Then a workflow stops assembling agents ad hoc:

```python
await runner.agent("Debug this reconciliation", preset="reconciliation-debugger")
# → uses the crystallized toolset + provider order, no on-the-fly tool selection
```

This is the **calcification pattern**: experience (logs) → a confirmed, recurring
pattern → a deterministic preset. It mirrors how `learn.py` already calcifies
provider reliability; crystallization extends it to the *whole agent shape*
(role + toolset + provider).

### Phasing

1. **Log everything** — done (events + enriched ledger).
2. **Analytics** — done (`run_summary`/`provider_stats`/`iworkflow stats`);
   add tokens/cost next.
3. **Propose presets** — `crystallize()` surfaces candidate presets; a human (or a
   judge agent) confirms them into a `presets.json`. *Semi-automatic first* — don't
   auto-adopt a pattern from thin data.
4. **Route to presets** — `agent(preset=…)`, and eventually auto-match a task to
   the best preset (the inverse of on-the-fly selection).

## Open questions

- Minimum sample size + recency for a stable preset (drift: catalogs/models change).
- Confirm presets human-in-the-loop vs auto — start human/judge-gated.
- Where presets live: generic `presets.json` in iworkflow vs host-project-owned
  (same split as the catalog — keep iworkflow generic).
- Privacy/size of logging task text; retention/rotation of `events.jsonl`.

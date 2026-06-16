# Design: the supervisor (adaptive re-planning)

Status: shipped.

The aim: let a workflow **adapt mid-run** — when reviews surface issues, when state
deviates from expectation, when a finding warrants more (or less) work — without
giving up the two things that make iworkflow what it is: **deterministic control
flow** (zero coordination tokens) and a spec that's **data, not code** (safe to
accept from an agent over MCP).

This is the answer to the one thing a frozen spec can't do. The engine already had
two partial precedents: a `gate` agent that can **abort**, and a `loop … until:
{agent|vote}` whose critic decides **stop/continue**. The supervisor is the next
rung: from `{stop | continue}` to **`{continue | reshape the plan | abort}`**.

It's also the safe, local answer to the "swarm orchestrator that *programs the next
sub-plan*" pattern (Slate's "programming in action-space" / Recursive Language
Models): instead of an always-on LLM kernel emitting a TypeScript DSL and spending
tokens to coordinate every step, a `supervisor` step fires only where the spec puts
it, and what it emits is **validated JSON the deterministic executor applies** — not
code it runs.

## The shape

A new top-level step kind, `supervisor`. Its coordinator agent receives the
accumulated run state and returns a `SUPERVISION_SCHEMA` decision:

```jsonc
{
  "action": "continue | adjust | abort",   // required
  "reason": "string",
  "skip":   ["step_id", ...],               // future steps to drop
  "set_params": { "k": v },                 // overlay onto params (future templating)
  "inject": [ { /* a step spec */ }, ... ]  // new steps, re-parsed under the same Limits
}
```

State is exposed to the coordinator's prompt as:

- `{{supervisor.steps}}` — completed steps' values (filtered by an optional `watch`
  list of ids),
- `{{supervisor.remaining}}` — ids still ahead in the plan,
- `{{supervisor.params}}` — current params.

## The `when` deviation guard

By default the coordinator always fires and its prompt decides. An optional `when`
predicate gates whether it fires at all — so the **on-track path spends zero
coordinator tokens**, and the agent runs only when state actually deviates:

```jsonc
"when": { "any": [
  { "path": "steps.fan.value", "select": "value.verdict", "in": ["ISSUES"] },
  { "path": "steps.scan.value.findings", "select": "severity", "gte": 8 }
] }
```

It's a small declarative predicate language — **data, not code**, evaluated against
the same accumulated ctx the prompt sees (safe to accept over MCP):

- **Leaf**: `{"path": "<dotted>", <op>: operand}` where `op` ∈ `eq` `ne` `in` `nin`
  `gte` `lte` `gt` `lt` `contains` `truthy` `exists`. An optional `select` sub-path
  is applied to each element when `path` resolves to a list; a leaf over a list is
  satisfied when **any** element matches (so "any review is ISSUES" is one leaf).
- **Combinators**: `{"all": [...]}`, `{"any": [...]}`, `{"not": pred}`.

Validated at parse time (exactly one operator per leaf, non-empty combinators), so a
malformed guard fails fast. When the guard is false the supervisor returns a no-op
`continue` decision (recorded with `skipped_guard: true`) — journaled, so a resume
replays the same no-op. This is the deterministic, cheap form of "fire on deviation";
the coordinator's *prompt* still makes the nuanced call once it does fire.

## Invariants (why this stays safe + deterministic)

1. **Only the tail is mutable.** A decision touches steps *after* the supervisor;
   the past is immutable. `skip` filters only future ids; `inject` inserts right
   after the supervisor.
2. **Injected steps can't escalate.** Each `inject` item is re-parsed through the
   same `_parse_step(..., limits=self.limits)` as any spec — so the `read-only`
   sandbox default and the tool-injection gate apply unchanged. A malformed or
   policy-violating inject **degrades gracefully** (dropped, recorded in
   `applied.errors`), the same "drop one, keep the batch" ethos as `pipeline()`.
3. **Bounded.** `Limits.max_supervisions` (default 8) caps how many times a
   supervisor may mutate the plan — cutting any supervisor→inject-supervisor→…
   recursion. Agent calls still count against `max_total_agent_calls`.
4. **Journaled + resumable.** The decision is stored in the step result. On resume,
   reaching the (already-journaled) supervisor **re-applies its recorded decision
   deterministically, without calling the agent** — re-injecting/re-skipping from
   the stored data; the injected steps, themselves journaled in the prior run,
   short-circuit to their results. Net: a resume reproduces the exact mutated plan
   with **zero new provider calls**.

## Where it lives

- `iworkflow/workflow.py` — `SUPERVISION_SCHEMA`; `Limits.max_supervisions`; the
  `supervisor` parse branch (top-level only); a **mutable `self.plan`** that `run()`
  walks by index (so the tail can grow/shrink); `_exec_supervisor` /
  `_apply_supervision` / `_supervisor_state`.
- `iworkflow/recipes.py` — `adaptive_review` (gate → fan two reviews → supervisor
  that injects a deep adversarial audit only when a review returns ISSUES/high).
- `tests/test_supervisor.py` — continue / skip / inject / set_params / abort /
  top-level-only / `max_supervisions` / privileged-inject-rejected / resume-replays-
  injection / state-exposed, all deterministic via a `ScriptedProvider`.

## Trade-off

A mutable plan reintroduces *some* non-determinism (the plan is no longer fixed at
parse time). It's bounded, validated, tail-only, and journaled — so it's
*controlled, auditable* non-determinism, recoverable from the journal, not the
open-ended self-direction of an LLM kernel. That's the deliberate line: enough
adaptivity to react to what a run discovers, not so much that a run stops being
reproducible.

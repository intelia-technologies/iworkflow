"""Capability-aware default routing.

When a workflow calls `agent(prompt)` WITHOUT an explicit provider, the runner
asks this module which subscription is best suited — grounded in what each model
is actually good at, and in scarcity (Claude shares one weekly subscription
limit, so it's reserved for the work that genuinely needs it).

These are PRIORS (general knowledge of the models), not measured on your tasks.
They're deliberately just a table + a small inference fn so you can watch the
routing decisions (logged) and tune them. Later this can be made empirical:
the journal already records provider+latency+verdict per agent, so a learner
could promote whichever provider wins a given task-kind over time.
"""

from __future__ import annotations

# --- What each model is good at (the thing to tune) ----------------------
CAPABILITIES = {
    "codex": {
        "model": "GPT-5.x-codex (codex exec, ChatGPT login)",
        "great_at": ["code implementation", "spec-following codegen", "refactors",
                     "tool use", "structured output"],
        "weak_at": ["very long context", "open-ended prose"],
        "structured_output": "native (--output-schema)",
        "context": "large",
        "billing": "ChatGPT subscription — abundant",
        "scarcity": "low",
    },
    "gemini": {
        "model": "Gemini (agy / Antigravity, Google login)",
        "great_at": ["1M+ context sweeps", "broad codebase analysis",
                     "fast classification", "independent adversarial audit"],
        "weak_at": ["native structured output (none)", "fine-grained code edits"],
        "structured_output": "none (JSON-block fallback)",
        "context": "1M+",
        "billing": "Google subscription — abundant",
        "scarcity": "low",
    },
    "claude": {
        "model": "Claude Opus/Sonnet (interactive TUI via tmux, Pool 1)",
        "great_at": ["deep reasoning", "delicate refactors", "architecture & judgment",
                     "nuanced writing", "careful final integration"],
        "weak_at": ["being abundant — shared weekly subscription limit"],
        "structured_output": "pane extraction (interactive)",
        "context": "1M",
        "billing": "subscription Pool 1 — SHARED WEEKLY LIMIT, scarce",
        "scarcity": "high — reserve for delicate / high-value work",
    },
}

# --- Task-kind → ordered provider preference -----------------------------
# Claude is USED where it earns its scarce quota (reason/write/core), and kept
# OUT of bulk fan-out (structured/classify/review/audit go to Codex/Gemini).
KIND_ROUTES = {
    # capability kinds
    "implement":  ["codex", "claude", "gemini"],   # codegen → codex; delicate → claude
    "structured": ["codex", "gemini", "claude"],   # native schema → codex
    "review":     ["codex", "gemini"],
    "audit":      ["gemini", "codex"],             # independent, schema-less → gemini
    "sweep":      ["gemini", "claude"],            # huge context → gemini's 1M
    "classify":   ["gemini", "codex"],             # cheap / fast
    "reason":     ["claude", "codex"],             # delicate judgment → worth Claude
    "write":      ["claude", "gemini"],            # prose → Claude
    "default":    ["codex", "gemini"],             # cheap, abundant, capable
    # legacy role aliases (back-compat with existing workflows)
    "doer":       ["codex", "gemini"],
    "core":       ["claude", "codex"],
    "auditor":    ["gemini", "codex"],
    "sweep_":     ["gemini", "claude"],
}

_LARGE_PROMPT = 30_000   # chars → treat as a context sweep
# Ordered precedence — first matching group wins. A present `schema` is the
# fallback for structured-but-unkeyworded tasks (→ codex native schema), checked
# AFTER the explicit-intent keywords so e.g. an "audit ... return JSON" still
# routes to the adversary, not the structured doer.
_KW = [
    (("adversarial", "refute", "skeptic", "audit", "red team"), "audit"),
    (("release notes", "changelog", "documentation", "draft ", "compose",
      "blog post", "email to", "prose"), "write"),
    (("implement", "write code", "refactor", "fix the", "add a function",
      "add an endpoint"), "implement"),
    (("classify", "categor", "label each", "triage"), "classify"),
    (("architecture", "trade-off", "should we", "decide", "design the"), "reason"),
    (("review", "assess", "check whether", "find bugs"), "review"),
]


def infer_kind(prompt: str, schema: dict | None) -> str:
    """Best-effort task kind when the workflow didn't say."""
    if len(prompt) > _LARGE_PROMPT:
        return "sweep"
    low = prompt.lower()
    for needles, kind in _KW:
        if any(n in low for n in needles):
            return kind
    return "structured" if schema else "default"


def route(role: str | None, *, schema: dict | None, prompt: str,
          available: list[str]) -> tuple[list[str], str]:
    """Return (ordered providers restricted to `available`, decision_label)."""
    if role and role in KIND_ROUTES:
        order, why = KIND_ROUTES[role], f"role={role}"
    elif role:
        order, why = KIND_ROUTES["default"], f"role={role}?→default"
    else:
        kind = infer_kind(prompt, schema)
        order, why = KIND_ROUTES[kind], f"inferred={kind}"
    picked = [p for p in order if p in available]
    if not picked:                       # nothing preferred is available
        picked = list(available)
        why += "→any"
    return picked, why

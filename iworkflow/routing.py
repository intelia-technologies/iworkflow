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

import json
from pathlib import Path

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
        "model": "Claude Opus/Sonnet (claude -p headless, subscription)",
        "great_at": ["deep reasoning", "delicate refactors", "architecture & judgment",
                     "nuanced writing", "careful final integration"],
        "weak_at": ["being abundant — shared weekly subscription limit"],
        "structured_output": "native (--json-schema)",
        "context": "1M",
        "billing": "subscription — SHARED WEEKLY LIMIT, scarce",
        "scarcity": "high — reserve for delicate / high-value work",
    },
    "cursor": {
        "model": "Composer 2.5 (fast tier deliberately unused — extra cost)",
        "great_at": ["repo-aware codegen", "fast implementation", "tool use",
                     "review in project context"],
        "weak_at": ["native structured output (none)", "headless without login"],
        "structured_output": "none (JSON-block fallback)",
        "context": "large",
        "billing": "Cursor subscription",
        "scarcity": "medium",
    },
}

# Load dynamic capabilities config override
MODELS_FILE = Path.home() / ".iworkflow" / "models.json"
if MODELS_FILE.is_file():
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "capabilities" in data:
            CAPABILITIES.update(data["capabilities"])
    except Exception:
        pass

# --- Task-kind → ordered provider preference -----------------------------
# Claude is USED where it earns its scarce quota (reason/write/core), and kept
# OUT of bulk fan-out (structured/classify/review/audit go to Codex/Gemini).
KIND_ROUTES = {
    # capability kinds
    "implement":  ["codex", "cursor", "claude", "gemini"],   # codegen → codex; delicate → claude
    "structured": ["codex", "gemini", "cursor", "claude"],   # native schema → codex
    "review":     ["codex", "cursor", "gemini"],
    "audit":      ["gemini", "codex", "cursor"],             # independent, schema-less → gemini
    "sweep":      ["gemini", "claude", "cursor"],            # huge context → gemini's 1M
    "classify":   ["cursor", "gemini", "codex"],             # cheap / fast (cursor→flash via hints)
    "reason":     ["claude", "cursor", "codex"],             # delicate judgment → worth Claude
    "write":      ["claude", "cursor", "gemini"],            # prose → Claude
    "default":    ["codex", "cursor", "gemini"],             # cheap, abundant, capable
    # legacy role aliases (back-compat with existing workflows)
    "doer":       ["codex", "gemini"],
    "core":       ["claude", "codex"],
    "auditor":    ["gemini", "codex"],
    "sweep_":     ["gemini", "claude"],
}

# Per kind, optional default model when a provider is auto-routed (vendor id → model id).
# cursor's -fast tier costs extra for the same model — hints always pin base.
KIND_MODEL_HINTS: dict[str, dict[str, str]] = {
    "audit": {"cursor": "composer-2.5"},
    "classify": {"cursor": "composer-2.5", "gemini": "Gemini 3.5 Flash (Medium)"},
    "default": {"cursor": "composer-2.5"},
    "implement": {"cursor": "composer-2.5"},
    "review": {"cursor": "composer-2.5"},
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
          available: list[str]) -> tuple[list[tuple[str, str | None]], str]:
    """Return (ordered (provider, model) targets, decision_label).

    Model may be None → use the provider CLI default or agent-level override.
  """
    from .provider_models import LEGACY_PROVIDER_ALIASES, resolve_model

    if role and role in KIND_ROUTES:
        order, why = KIND_ROUTES[role], f"role={role}"
        hint_key = role
    elif role:
        order, why = KIND_ROUTES["default"], f"role={role}?→default"
        hint_key = "default"
    else:
        kind = infer_kind(prompt, schema)
        order, why = KIND_ROUTES[kind], f"inferred={kind}"
        hint_key = kind
    hints = KIND_MODEL_HINTS.get(hint_key, {})
    picked: list[tuple[str, str | None]] = []
    for name in order:
        if name not in available:
            continue
        if name in LEGACY_PROVIDER_ALIASES:
            prov, model = LEGACY_PROVIDER_ALIASES[name]
            if prov in available:
                picked.append((prov, resolve_model(prov, model)))
            continue
        picked.append((name, resolve_model(name, hints.get(name))))
    if not picked:
        picked = [(p, None) for p in available]
        why += "→any"
    return picked, why

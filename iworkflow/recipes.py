"""Named workflow recipes — the "predefined" door.

A recipe is just a declarative spec (see workflow.py) registered under a name, so
the same engine runs both built-in recipes (`workflow="review"`) and a caller's
ad-hoc spec. Host projects drop their own `*.json` specs into `.iworkflow/recipes/`
and they show up alongside the built-ins — iworkflow stays domain-agnostic.

The calcification path: a dynamic spec that proves useful is saved here as a
named recipe (dynamic → confirmed → preset).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .workflow import DECISION_SCHEMA

# --- shared schemas -------------------------------------------------------
GATE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "BLOCKED"]},
                   "summary": {"type": "string"},
                   "risks": {"type": "array", "items": {"type": "string"}}},
}
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {"verdict": {"type": "string", "enum": ["PASS", "ISSUES"]},
                   "severity": {"type": "string", "enum": ["low", "med", "high"]},
                   "findings": {"type": "array", "items": {"type": "string"}}},
}
FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["title"],
        "properties": {"title": {"type": "string"}, "detail": {"type": "string"},
                       "severity": {"type": "string"}}}}},
}
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["proposals"],
    "properties": {"proposals": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "rationale"],
        "properties": {"title": {"type": "string"}, "rationale": {"type": "string"},
                       "effort": {"type": "string", "enum": ["S", "M", "L"]}}}}},
}


# --- built-in recipes (specs) --------------------------------------------
FAN_SYNTHESIZE: dict[str, Any] = {
    "name": "fan_synthesize",
    "description": "Two proposers from different angles, then one decisive synthesis.",
    "params": {"goal": None},
    "steps": [
        {"id": "fan", "kind": "parallel", "agents": [
            {"id": "eng", "prefer": ["codex", "gemini"],
             "prompt": "From an engineering angle, answer concisely: {{params.goal}}"},
            {"id": "dx", "prefer": ["gemini", "codex"],
             "prompt": "From a product/UX angle, answer concisely: {{params.goal}}"},
        ]},
        {"id": "synth", "kind": "agent", "needs": ["fan"], "prefer": ["codex", "claude"],
         "prompt": "Synthesize ONE decisive answer to: {{params.goal}}\n"
                   "Inputs: {{steps.fan.value}}"},
    ],
    "output": "{{steps.synth.value}}",
}

REVIEW: dict[str, Any] = {
    "name": "review",
    "description": "Gate the design, fan two disjoint reviews, then an independent "
                   "adversarial audit that ignores the reviewers.",
    "params": {"topic": None, "subject_a": None, "subject_b": None},
    "schemas": {"gate": GATE_SCHEMA, "review": REVIEW_SCHEMA},
    "steps": [
        {"id": "gate", "kind": "agent", "schema": "gate", "prefer": ["claude", "codex"],
         "gate": {"field": "verdict", "abort_on": "BLOCKED"},
         "prompt": "Assess whether {{params.topic}} is sound enough to review in "
                   "detail. verdict=DONE to proceed, BLOCKED if fundamentally broken."},
        {"id": "fan", "kind": "parallel", "needs": ["gate"], "agents": [
            {"id": "a", "schema": "review", "prefer": ["codex", "gemini"],
             "prompt": "Review this for CORRECTNESS. verdict=PASS/ISSUES.\n{{params.subject_a}}"},
            {"id": "b", "schema": "review", "prefer": ["gemini", "codex"],
             "prompt": "Review this for ROBUSTNESS. verdict=PASS/ISSUES.\n{{params.subject_b}}"},
        ]},
        {"id": "audit", "kind": "agent", "needs": ["fan"], "prefer": ["gemini", "codex"],
         "prompt": "Independent adversarial auditor. Trust NO other reviewer. Is there "
                   "a real correctness hole across these?\n{{params.subject_a}}\n"
                   "{{params.subject_b}}"},
    ],
    "output": {"gate": "{{steps.gate.value}}", "reviews": "{{steps.fan.value}}",
               "audit": "{{steps.audit.value}}"},
}

ROADMAP: dict[str, Any] = {
    "name": "roadmap",
    "description": "Each provider proposes from its strength, then one prioritizes.",
    "params": {"context": None},
    "schemas": {"proposal": PROPOSAL_SCHEMA},
    "steps": [
        {"id": "propose", "kind": "parallel", "agents": [
            {"id": "eng", "schema": "proposal", "prefer": ["codex", "gemini"],
             "prompt": "{{params.context}}\n\nAs an engineering-robustness reviewer, "
                       "propose the 3 highest-impact next improvements."},
            {"id": "dx", "schema": "proposal", "prefer": ["gemini", "codex"],
             "prompt": "{{params.context}}\n\nAs a capabilities/DX reviewer, propose the "
                       "3 most valuable next improvements."},
        ]},
        {"id": "synth", "kind": "agent", "needs": ["propose"], "prefer": ["claude", "codex"],
         "prompt": "Merge these proposals into ONE prioritized roadmap (rank, title, "
                   "one-line why, effort S/M/L). Dedupe; highest-leverage first.\n"
                   "{{steps.propose.value}}"},
    ],
    "output": "{{steps.synth.value}}",
}

DEEP_REVIEW: dict[str, Any] = {
    "name": "deep_review",
    "description": "Loop: find new issues each round until a critic agent judges the "
                   "review complete (hard-capped). Showcases an agent-decided loop.",
    "params": {"subject": None},
    "schemas": {"decision": DECISION_SCHEMA, "findings": FINDINGS_SCHEMA},
    "steps": [
        {"id": "sweep", "kind": "loop", "max_iterations": 4,
         "until": {"agent": {
             "prompt": "Findings so far: {{loop.collected}}. Is the review of "
                       "{{params.subject}} complete? verdict=STOP if nothing material is "
                       "missing, else CONTINUE and list what's missing.",
             "stop_when": "STOP", "prefer": ["gemini", "claude"]}},
         "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
         "body": [
             {"id": "find", "kind": "agent", "schema": "findings", "prefer": ["codex", "gemini"],
              "prompt": "Find NEW issues in {{params.subject}} NOT already in: "
                        "{{loop.collected}}. Prioritise what the critic flagged missing: "
                        "{{loop.decision.missing}}."},
         ]},
    ],
    "output": "{{steps.sweep.value}}",
}

BUILTIN: dict[str, dict[str, Any]] = {
    spec["name"]: spec for spec in (FAN_SYNTHESIZE, REVIEW, ROADMAP, DEEP_REVIEW)
}

DEFAULT_RECIPE_DIR = ".iworkflow/recipes"


def _load_dir(extra_dir: str | None) -> dict[str, dict[str, Any]]:
    """Load host-project `*.json` specs (each a recipe) from a directory."""
    found: dict[str, dict[str, Any]] = {}
    root = Path(extra_dir or DEFAULT_RECIPE_DIR)
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(spec, dict) and isinstance(spec.get("steps"), list):
            found[spec.get("name") or path.stem] = spec
    return found


def all_recipes(extra_dir: str | None = None) -> dict[str, dict[str, Any]]:
    """Built-in recipes overlaid with host-project recipes (host wins on name)."""
    return {**BUILTIN, **_load_dir(extra_dir)}


def list_recipes(extra_dir: str | None = None) -> list[dict[str, Any]]:
    return [{"name": name, "description": spec.get("description"),
             "params": sorted((spec.get("params") or {}).keys())}
            for name, spec in all_recipes(extra_dir).items()]


def get_recipe(name: str, extra_dir: str | None = None) -> dict[str, Any]:
    recipes = all_recipes(extra_dir)
    if name not in recipes:
        raise KeyError(f"unknown recipe {name!r}; known: {sorted(recipes)}")
    return recipes[name]

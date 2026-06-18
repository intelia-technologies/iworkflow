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

THREAT_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["threats"],
    "properties": {
        "threats": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["title", "severity", "description"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "description": {"type": "string"}
                }
            }
        }
    }
}

SECURITY_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["file", "vulnerability", "risk_rating"],
                "properties": {
                    "file": {"type": "string"},
                    "vulnerability": {"type": "string"},
                    "risk_rating": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]}
                }
            }
        }
    }
}

PATCH_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["patched_file", "success", "regressions_found"],
    "properties": {
        "patched_file": {"type": "string"},
        "success": {"type": "boolean"},
        "regressions_found": {"type": "boolean"},
        "regression_details": {"type": "string"}
    }
}


# --- built-in recipes (specs) --------------------------------------------
FAN_SYNTHESIZE: dict[str, Any] = {
    "name": "fan_synthesize",
    "description": "Two proposers from different angles, then one decisive synthesis.",
    "params": {"goal": None},
    "steps": [
        {"id": "fan", "kind": "parallel", "agents": [
            {"id": "eng", "prefer": ["cursor:flash", "codex", "gemini"],
             "prompt": "From an engineering angle, answer concisely: {{params.goal}}"},
            {"id": "dx", "prefer": ["gemini", "cursor:flash", "codex"],
             "prompt": "From a product/UX angle, answer concisely: {{params.goal}}"},
        ]},
        {"id": "synth", "kind": "agent", "needs": ["fan"],
         "required": False,
         "prefer": ["cursor:composer-2.5", "codex", "gemini", "claude"],
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

ADAPTIVE_REVIEW: dict[str, Any] = {
    "name": "adaptive_review",
    "description": "Gate, fan two reviews, then a SUPERVISOR inspects the findings and "
                   "either passes through or injects a deep adversarial audit — the "
                   "coordinator adapts the plan to what the reviews actually surfaced.",
    "params": {"topic": None, "subject_a": None, "subject_b": None},
    "schemas": {"gate": GATE_SCHEMA, "review": REVIEW_SCHEMA},
    "steps": [
        {"id": "gate", "kind": "agent", "schema": "gate", "prefer": ["claude", "codex"],
         "gate": {"field": "verdict", "abort_on": "BLOCKED"},
         "prompt": "Assess whether {{params.topic}} is sound enough to review in detail. "
                   "verdict=DONE to proceed, BLOCKED if fundamentally broken."},
        {"id": "fan", "kind": "parallel", "needs": ["gate"], "agents": [
            {"id": "a", "schema": "review", "prefer": ["codex", "gemini"],
             "prompt": "Review this for CORRECTNESS. verdict=PASS/ISSUES.\n{{params.subject_a}}"},
            {"id": "b", "schema": "review", "prefer": ["gemini", "codex"],
             "prompt": "Review this for ROBUSTNESS. verdict=PASS/ISSUES.\n{{params.subject_b}}"},
        ]},
        {"id": "supervise", "kind": "supervisor", "needs": ["fan"],
         "prefer": ["claude", "codex"], "watch": ["fan"],
         # only fire the coordinator when a review actually flags something — the
         # clean path (both PASS, low severity) spends zero coordinator tokens.
         "when": {"any": [
             {"path": "steps.fan.value", "select": "value.verdict", "in": ["ISSUES"]},
             {"path": "steps.fan.value", "select": "value.severity", "eq": "high"}]},
         "prompt": "You coordinate a code review. The two reviews returned:\n"
                   "{{supervisor.steps}}\n\nSubjects under review:\nA: {{params.subject_a}}\n"
                   "B: {{params.subject_b}}\n\nIf EITHER review verdict is ISSUES (or severity "
                   "is high), set action=adjust and inject exactly one deep-audit step — "
                   "inject=[{\"id\":\"audit\",\"kind\":\"agent\",\"prefer\":[\"gemini\","
                   "\"codex\"],\"prompt\":\"<a concrete adversarial-audit instruction naming "
                   "the specific correctness risk you want chased down>\"}]. If both reviews "
                   "PASS cleanly, set action=continue."},
    ],
    "output": {"gate": "{{steps.gate.value}}", "reviews": "{{steps.fan.value}}",
               "supervision": "{{steps.supervise.value}}", "audit": "{{steps.audit.value}}"},
}

COMPLEX_SECURITY_AUDIT: dict[str, Any] = {
    "name": "complex_security_audit",
    "description": "Multi-stage automated security audit workflow: scopes/gates input, models threats in parallel, scans modules via a pipeline, loops to generate and validate fixes, and uses a supervisor to dynamically adjust final review actions.",
    "params": {
        "modules": ["auth-service", "billing-core", "gateway-api"],
        "codebase_description": "Microservice architecture handling payments and OAuth2 auth flows."
    },
    "schemas": {
        "gate": GATE_SCHEMA,
        "threat_model": THREAT_MODEL_SCHEMA,
        "findings": SECURITY_FINDINGS_SCHEMA,
        "patch_result": PATCH_RESULT_SCHEMA,
        "decision": DECISION_SCHEMA
    },
    "steps": [
        {
            "id": "scope_check",
            "kind": "agent",
            "schema": "gate",
            "prefer": ["claude"],
            "gate": {"field": "verdict", "abort_on": "BLOCKED"},
            "prompt": "Verify if the scope is well-defined. Modules to scan: {{params.modules}}. Context: {{params.codebase_description}}. verdict=READY to proceed, BLOCKED if parameters are empty or invalid."
        },
        {
            "id": "threat_modeling",
            "kind": "parallel",
            "needs": ["scope_check"],
            "agents": [
                {
                    "id": "data_flow",
                    "prefer": ["gemini"],
                    "schema": "threat_model",
                    "prompt": "Analyze data flow boundaries and trust zones for: {{params.codebase_description}}. Propose threats with severity ratings."
                },
                {
                    "id": "abuse_cases",
                    "prefer": ["codex"],
                    "schema": "threat_model",
                    "prompt": "Analyze abuse cases and logical exploit paths for: {{params.codebase_description}}."
                }
            ]
        },
        {
            "id": "module_scanning",
            "kind": "pipeline",
            "needs": ["threat_modeling"],
            "items": "{{params.modules}}",
            "stages": [
                {
                    "id": "dependency_check",
                    "prefer": ["codex"],
                    "schema": "findings",
                    "prompt": "Search for known vulnerable packages or unsafe dependencies in module: {{item}}."
                },
                {
                    "id": "static_analysis",
                    "prefer": ["gemini"],
                    "schema": "findings",
                    "prompt": "Identify code-level vulnerabilities (e.g. hardcoded secrets, injection, unsafe functions) in module: {{item}}."
                }
            ]
        },
        {
            "id": "remediation_loop",
            "kind": "loop",
            "needs": ["module_scanning"],
            "max_iterations": 3,
            "until": {
                "agent": {
                    "prompt": "Assess accumulated patches so far: {{loop.collected}}. Are all high-risk items fixed? verdict=STOP if complete, else CONTINUE and specify what's missing.",
                    "stop_when": "STOP",
                    "prefer": ["claude"]
                }
            },
            "collect": {"from": "validate_patch", "path": "patched_file"},
            "body": [
                {
                    "id": "generate_patch",
                    "kind": "agent",
                    "prefer": ["codex"],
                    "prompt": "Generate a remediation patch based on module scanning findings. Focused on: {{loop.decision.missing}}. Previous attempts: {{loop.collected}}."
                },
                {
                    "id": "validate_patch",
                    "kind": "agent",
                    "schema": "patch_result",
                    "prefer": ["gemini"],
                    "prompt": "Verify the patch from generate_patch (value: {{loop.last.generate_patch.value}}). Ensure no regressions are introduced."
                }
            ]
        },
        {
            "id": "final_coordinator",
            "kind": "supervisor",
            "needs": ["remediation_loop"],
            "watch": ["threat_modeling", "remediation_loop"],
            "prefer": ["claude"],
            "when": {"path": "steps.remediation_loop", "select": "stop_reason", "eq": "max_iterations"},
            "prompt": "Review the full audit results. If remediation_loop has failed or is incomplete, inject a manual_triage step. Otherwise, set action=continue."
        }
    ],
    "output": {
        "scope_status": "{{steps.scope_check.value}}",
        "threats_found": "{{steps.threat_modeling.value}}",
        "scanned_findings": "{{steps.module_scanning.value}}",
        "patches_applied": "{{steps.remediation_loop.value}}",
        "supervision_action": "{{steps.final_coordinator.value}}",
        "manual_triage_result": "{{steps.manual_triage.value}}"
    }
}

BRAINSTORM: dict[str, Any] = {
    "name": "brainstorm",
    "description": "Interactive brainstorming workflow with context inspection and spec generation.",
    "execution": {
        "worktree": "new:brainstorm-{{params.change_name}}",
        "branch": "new:brainstorm/{{params.change_name}}",
        "gh_required": True
    },
    "params": {
        "change_name": "feature-x",
        "user_input": ""
    },
    "schemas": {
        "proposal": PROPOSAL_SCHEMA,
        "decision": DECISION_SCHEMA
    },
    "steps": [
        {
            "id": "phase1_search",
            "kind": "agent",
            "prefer": ["gemini"],
            "role": "researcher",
            "timeout_s": 120,
            "heartbeat_interval_s": 30,
            "tools": ["grep", "find"],
            "prompt": "Search the codebase and 'openspec/changes/' for any existing brainstorms related to: {{params.change_name}}. List them or confirm if none exist."
        },
        {
            "id": "phase2_clarification",
            "kind": "agent",
            "prefer": ["gemini", "codex", "claude"],
            "role": "architect",
            "timeout_s": 60,
            "heartbeat_interval_s": 15,
            "schema": {
                "type": "object", "required": ["questions"],
                "properties": {"questions": {"type": "array", "items": {"type": "string"}}}
            },
            "prompt": "Based on the task '{{params.change_name}}' and existing research '{{steps.phase1_search.value}}', generate 2-3 targeted clarification questions for the user to define the scope."
        },
        {
            "id": "phase3_context",
            "kind": "parallel",
            "agents": [
                {
                    "id": "inspect_code",
                    "prefer": ["gemini"],
                    "model": "pro",
                    "tools": ["grep", "find"],
                    "prompt": "Analyze existing code and specs related to {{params.change_name}}."
                },
                {
                    "id": "inspect_rules",
                    "prefer": ["gemini"],
                    "model": "flash",
                    "tools": ["read"],
                    "prompt": "Extract relevant 'learnings' and 'rules' from the project documentation."
                }
            ]
        },
        {
                    "id": "phase4_proposals",
                    "kind": "agent",
                    "prefer": ["gemini", "codex", "claude"],
                    "role": "solution_designer",
                    "schema": "proposal",
                    "timeout_s": 60,
                    "heartbeat_interval_s": 15,
                    "prompt": "Using context from {{steps.phase3_context.value}}, propose 2-3 technical approaches for {{params.change_name}}. Include honest pros/contras for each."
                },
        {
            "id": "phase5_dialogue_loop",
            "kind": "loop",
            "max_iterations": 5,
            "until": {
                "agent": {
                    "prompt": "Review the dialogue: {{loop.collected}}. Is the scope locked and all forks resolved? Return STOP or CONTINUE.",
                    "stop_when": "STOP",
                    "prefer": ["gemini", "codex", "claude"],
                    "timeout_s": 60,
                    "heartbeat_interval_s": 15
                }
            },
            "body": [
                {
                    "id": "chat",
                    "kind": "agent",
                    "prefer": ["gemini", "codex", "claude"],
                    "models": {"claude": "sonnet-3.5"},
                    "timeout_s": 60,
                    "heartbeat_interval_s": 15,
                    "prompt": "Current status: {{loop.collected}}. User says: {{params.user_input}}. Refine the direction until scope is locked."
                }
            ]
        },
        {
            "id": "phase6_write_spec",
            "kind": "agent",
            "prefer": ["codex", "gemini", "claude"],
            "model": "sonnet",
            "timeout_s": 90,
            "heartbeat_interval_s": 15,
            "tools": ["write"],
            "sandbox": "write",
            "instructions": { "gh": "gh pr create --title 'Brainstorm: {{params.change_name}}' --body 'Generated via iworkflow'" },
            "prompt": "Write the final brainstorm to 'openspec/changes/{{params.change_name}}/brainstorm.md' using the standard template. Context: {{steps.phase5_dialogue_loop.value}}."
        },
        {
            "id": "phase7_update_wiki",
            "kind": "agent",
            "prefer": ["codex", "gemini", "claude"],
            "model": "sonnet",
            "timeout_s": 90,
            "heartbeat_interval_s": 15,
            "tools": ["write"],
            "sandbox": "write",
            "prompt": "Update the wiki in 'thoughts/shared/wiki/' with the new domain knowledge from this brainstorm."
        },
        {
            "id": "phase8_handoff",
            "kind": "agent",
            "prefer": ["gemini", "codex", "claude"],
            "timeout_s": 60,
            "heartbeat_interval_s": 15,
            "prompt": "Confirm files created. Suggest next step: /workflows:plan {{params.change_name}}"
        },
        {
            "id": "brainstorm_monitor",
            "kind": "supervisor",
            "needs": ["phase5_dialogue_loop"],
            "watch": ["phase5_dialogue_loop"],
            "prefer": ["gemini", "codex", "claude"],
            "timeout_s": 60,
            "heartbeat_interval_s": 15,
            "when": {"path": "steps.phase5_dialogue_loop", "select": "stop_reason", "eq": "max_iterations"},
            "prompt": "The dialogue loop reached max iterations without locking the scope. Should we inject a manual triage step or continue as-is?"
        }
    ],
    "artifacts": [
        {"path": "openspec/changes/{{params.change_name}}/brainstorm.md", "type": "file"}
    ],
    "output": {
        "spec_path": "openspec/changes/{{params.change_name}}/brainstorm.md",
        "branch": "brainstorm/{{params.change_name}}"
    }
}

BUILTIN: dict[str, dict[str, Any]] = {
    spec["name"]: spec
    for spec in (FAN_SYNTHESIZE, REVIEW, ROADMAP, DEEP_REVIEW, ADAPTIVE_REVIEW, COMPLEX_SECURITY_AUDIT, BRAINSTORM)
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

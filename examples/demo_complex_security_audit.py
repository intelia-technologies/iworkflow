#!/usr/bin/env python3
"""Complex security audit workflow demo.

Demonstrates a rich security scan workflow featuring:
1. Gate check to scope the run.
2. Parallel threat modelers fanning out simultaneously.
3. Multi-stage pipeline mapping modules sequentially.
4. Auto-remediation loop generating & validating patches.
5. Supervisor dynamically injecting a triage agent on failure.

Run:  python3 examples/demo_complex_security_audit.py
      python3 examples/demo_complex_security_audit.py --live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from iworkflow import (  # noqa: E402
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    Limits,
    Provider,
    Runner,
    run_spec,
)
from iworkflow.toolsets import ToolSet  # noqa: E402

# 1. Declarative workflow specification
SPEC = {
    "name": "complex_security_audit",
    "description": "Multi-stage automated security audit workflow: scopes/gates input, models threats in parallel, scans modules via a pipeline, loops to generate and validate fixes, and uses a supervisor to dynamically adjust final review actions.",
    "params": {
        "modules": ["auth-service", "billing-core", "gateway-api"],
        "codebase_description": "Microservice architecture handling payments and OAuth2 auth flows."
    },
    "schemas": {
        "gate": {
            "type": "object",
            "required": ["verdict"],
            "properties": {
                "verdict": {"type": "string", "enum": ["READY", "BLOCKED"]},
                "summary": {"type": "string"}
            }
        },
        "threat_model": {
            "type": "object",
            "required": ["threats"],
            "properties": {
                "threats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "severity", "description"],
                        "properties": {
                            "title": {"type": "string"},
                            "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                            "description": {"type": "string"}
                        }
                    }
                }
            }
        },
        "findings": {
            "type": "object",
            "required": ["findings"],
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["file", "vulnerability", "risk_rating"],
                        "properties": {
                            "file": {"type": "string"},
                            "vulnerability": {"type": "string"},
                            "risk_rating": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]}
                        }
                    }
                }
            }
        },
        "patch_result": {
            "type": "object",
            "required": ["patched_file", "success", "regressions_found"],
            "properties": {
                "patched_file": {"type": "string"},
                "success": {"type": "boolean"},
                "regressions_found": {"type": "boolean"},
                "regression_details": {"type": "string"}
            }
        },
        "decision": {
            "type": "object",
            "required": ["verdict"],
            "properties": {
                "verdict": {"type": "string", "enum": ["CONTINUE", "STOP"]},
                "missing": {"type": "array", "items": {"type": "string"}}
            }
        }
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


# 2. Simulated/Scripted Provider for Offline Demonstration
class DemoScriptedProvider(Provider):
    def __init__(self, name: str, mode: str) -> None:
        super().__init__(name)
        self.mode = mode  # "success" or "failure"
        self._n = 0

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str,
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
    ) -> Any:
        self.last_usage = {"input_tokens": 10, "output_tokens": 20, "cost_usd": None}
        self._n += 1

        # Match prompts to return structured answers
        if "Verify if the scope is well-defined" in prompt:
            return {"verdict": "READY", "summary": "Scope verified."}

        if "Analyze data flow boundaries" in prompt:
            return {
                "threats": [
                    {
                        "title": "Insecure session storage",
                        "severity": "HIGH",
                        "description": "Session tokens stored in localstorage."
                    }
                ]
            }

        if "Analyze abuse cases" in prompt:
            return {
                "threats": [
                    {
                        "title": "Privilege escalation via gateway",
                        "severity": "MEDIUM",
                        "description": "API Gateway does not strip headers."
                    }
                ]
            }

        if "Search for known vulnerable packages" in prompt:
            if "auth-service" in prompt:
                return {
                    "findings": [
                        {
                            "file": "auth-service/package.json",
                            "vulnerability": "CVE-2026-1029 in jwt-library",
                            "risk_rating": "CRITICAL"
                        }
                    ]
                }
            return {"findings": []}

        if "Identify code-level vulnerabilities" in prompt:
            if "billing-core" in prompt:
                return {
                    "findings": [
                        {
                            "file": "billing-core/src/db.py",
                            "vulnerability": "Hardcoded database credential",
                            "risk_rating": "HIGH"
                        }
                    ]
                }
            return {"findings": []}

        if "Generate a remediation patch" in prompt:
            if "billing-core" in prompt:
                return "Moved hardcoded credential in billing-core to environment variable config."
            return "Upgraded jwt-library version to 3.4.1 in auth-service package.json."

        if "Verify the patch from generate_patch" in prompt:
            if "billing-core" in prompt:
                return {"patched_file": "billing-core/src/db.py", "success": True, "regressions_found": False}
            return {"patched_file": "auth-service/package.json", "success": True, "regressions_found": False}

        if "Assess accumulated patches so far" in prompt:
            if self.mode == "success":
                # In success mode: we stop as soon as we see billing-core is patched
                if "billing-core" in prompt:
                    return {"verdict": "STOP", "missing": []}
                return {"verdict": "CONTINUE", "missing": ["billing-core credential fix"]}
            else:
                # In failure mode: always ask to continue to hit max_iterations
                return {"verdict": "CONTINUE", "missing": ["auth-service vulnerability fix"]}

        if "Review the full audit results" in prompt:
            # Only called if when is satisfied
            return {
                "action": "adjust",
                "reason": "Remediation loop hit iteration limit with outstanding security flaws.",
                "inject": [
                    {
                        "id": "manual_triage",
                        "kind": "agent",
                        "prefer": ["claude"],
                        "prompt": "Vulnerabilities could not be fully patched automatically. Perform manual security triage on the remaining issues."
                    }
                ]
            }

        if "Perform manual security triage" in prompt:
            return "Remaining issues triaged manually. Escalated critical jwt-library issue to core security team."

        return {"status": "UNKNOWN_PROMPT"}


async def run_demo(mode: str, live: bool = False) -> None:
    print(f"\n==================================================")
    print(f"Running COMPLEX SECURITY AUDIT workflow ({mode.upper()} mode)")
    print(f"==================================================")

    journal_dir = f".iworkflow_demo_{mode}"
    shutil.rmtree(journal_dir, ignore_errors=True)

    if live:
        providers = {
            "codex": CodexProvider("codex", timeout_s=180),
            "gemini": GeminiProvider("gemini", timeout_s=180),
            "claude": ClaudeProvider("claude", timeout_s=180),
        }
    else:
        # One mock provider under all provider names
        p = DemoScriptedProvider("demo-provider", mode)
        providers = {"codex": p, "gemini": p, "claude": p}

    runner = Runner(
        run_id=f"audit_{mode}",
        providers=providers,
        caps={"codex": 2, "gemini": 2, "claude": 2},
        journal_dir=journal_dir,
    )

    try:
        # Run with allow_tools enabled locally
        result = await run_spec(runner, SPEC, limits=Limits(allow_tools=True))
        print("\nWorkflow Execution Results Bundle:")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

        if mode == "success":
            assert result["status"] == "DONE"
            assert len(result["steps"]["remediation_loop"]) == 2
            assert "manual_triage" not in result["steps"]
        else:
            assert result["status"] == "DONE"
            assert len(result["steps"]["remediation_loop"]) == 3
            assert result["steps"]["final_coordinator"]["action"] == "adjust"
            assert "manual_triage" in result["steps"]

        print(f"\n✅ {mode.upper()} scenario validated successfully!")
    finally:
        shutil.rmtree(journal_dir, ignore_errors=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run complex security audit workflow demo.")
    parser.add_argument("--live", action="store_true", help="Run with live subscription adapters.")
    args = parser.parse_args()

    # Save the JSON specification so it can be used with `iworkflow run` / `iworkflow graph`
    spec_path = ROOT / "examples" / "complex_security_audit.json"
    spec_path.write_text(json.dumps(SPEC, indent=2), encoding="utf-8")
    print(f"Saved declarative spec to: {spec_path}")

    # Run the offline fakes for both scenario paths (success and failure/escalation)
    if not args.live:
        await run_demo(mode="success", live=False)
        await run_demo(mode="failure", live=False)
        print("\nAll local offline demo simulations passed.")
        print("To run with live providers, use: python3 examples/demo_complex_security_audit.py --live")
    else:
        # Live run (user's active subscriptions)
        await run_demo(mode="success", live=True)


if __name__ == "__main__":
    asyncio.run(main())

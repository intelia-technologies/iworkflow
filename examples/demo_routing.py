#!/usr/bin/env python3
"""Show the capability-aware default routing — OFFLINE, no quota spent.

Prints, for a set of sample tasks, the inferred kind and the provider order the
scheduler would pick when the workflow gives no explicit provider. Use it to see
and tune the defaults in iworkflow/routing.py.

Run:  python3 examples/demo_routing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from iworkflow import CAPABILITIES, infer_kind, route  # noqa: E402

AVAILABLE = ["codex", "gemini", "claude"]
SCHEMA = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}

SAMPLES = [
    ("implement a paginated FastAPI endpoint for expenses", None, None),
    ("extract the verdict as JSON", SCHEMA, None),
    ("adversarially audit whether this failover design has a hole", None, None),
    ("classify each of these 200 transactions as work or personal", None, None),
    ("should we use event sourcing here? weigh the trade-offs", None, None),
    ("write the user-facing release notes for this change", None, None),
    ("review this module for bugs", None, None),
    ("summarize this entire 1M-token codebase" + " x" * 16000, None, None),
    ("do the thing", None, None),                       # falls to default
    ("anything", None, "core"),                         # explicit role
]


def main():
    print("═══ What each model is good at ═══")
    for name, c in CAPABILITIES.items():
        print(f"  {name:7} [{c['scarcity']:>4} scarcity] {c['billing']}")
        print(f"          great at: {', '.join(c['great_at'][:3])}…")
    print("\n═══ Default routing decisions (no explicit provider) ═══")
    print(f"  available: {AVAILABLE}\n")
    for prompt, schema, role in SAMPLES:
        order, why = route(role, schema=schema, prompt=prompt, available=AVAILABLE)
        kind = role or infer_kind(prompt, schema)
        label = (prompt[:46] + "…") if len(prompt) > 46 else prompt
        print(f"  {label:<48} → {str(order):<28} ({why})")


if __name__ == "__main__":
    main()

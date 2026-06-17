"""Supervisor step — the coordinator that adapts the plan mid-run.

Deterministic, zero-quota: a ScriptedProvider routes by schema (the `supervision`
shape carries `action`) so a test can drive continue / adjust / abort decisions
exactly. Mirrors the patterns in test_workflow.py.
"""

import asyncio

import pytest

from iworkflow import (
    Limits, Provider, Runner, WorkflowLimitError, get_recipe, list_recipes, run_spec,
)
from iworkflow.workflow import WorkflowError, WorkflowSpec


class ScriptedProvider(Provider):
    """Returns whatever `responder(prompt, schema, call_index)` decides."""

    def __init__(self, name, responder):
        super().__init__(name)
        self.responder = responder
        self._n = 0

    async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None, model=None):
        self.last_usage = {"input_tokens": 1, "output_tokens": 5, "cost_usd": None}
        i = self._n
        self._n += 1
        return self.responder(prompt, schema, i)


def _scripted_runner(tmp_path, responder, run_id="sup"):
    # one provider object under three names → shared call sequence, any prefer routes to it
    p = ScriptedProvider("codex", responder)
    runner = Runner(run_id, {"codex": p, "gemini": p, "claude": p},
                    {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    return runner, p


def _run(coro):
    return asyncio.run(coro)


def _is_supervisor_call(schema):
    return "action" in (schema or {}).get("properties", {})


# --- continue -------------------------------------------------------------
def test_supervisor_continue_runs_downstream(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "continue"}
        return f"ran:{prompt}"

    runner, p = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "decide"},
        {"id": "after", "kind": "agent", "needs": ["sup"], "prefer": ["codex"], "prompt": "go"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert out["steps"]["sup"]["action"] == "continue"
    assert out["steps"]["after"] == "ran:go"          # downstream ran unchanged
    assert p._n == 2


# --- adjust: skip ---------------------------------------------------------
def test_supervisor_adjust_skips_future_step(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "adjust", "skip": ["after"]}
        return "ran"

    runner, p = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "decide"},
        {"id": "after", "kind": "agent", "needs": ["sup"], "prefer": ["codex"], "prompt": "go"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert "after" not in out["steps"]                # future step pruned
    assert p._n == 1                                  # only the supervisor ran


# --- adjust: inject -------------------------------------------------------
def test_supervisor_adjust_injects_step(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "adjust", "inject": [
                {"id": "extra", "kind": "agent", "prefer": ["codex"], "prompt": "injected work"}]}
        return f"ran:{prompt}"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "d"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert out["steps"]["extra"] == "ran:injected work"   # injected step executed


# --- adjust: set_params ---------------------------------------------------
def test_supervisor_adjust_sets_params(tmp_path):
    seen = {}

    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "adjust", "set_params": {"q": "NEW"}}
        seen["prompt"] = prompt
        return "ran"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "decide"},
        {"id": "after", "kind": "agent", "needs": ["sup"], "prefer": ["codex"],
         "prompt": "use {{params.q}}"}],
        "params": {"q": "OLD"}}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert seen["prompt"] == "use NEW"                # overlay reached future templating


# --- abort ----------------------------------------------------------------
def test_supervisor_aborts(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "abort", "reason": "fundamentally broken"}
        return "ran"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "decide"},
        {"id": "after", "kind": "agent", "needs": ["sup"], "prefer": ["codex"], "prompt": "go"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "ABORTED"
    assert out["aborted_at"] == "sup"
    assert out["steps"]["sup"]["action"] == "abort"   # decision recorded before abort
    assert "after" not in out["steps"]


# --- parse: top-level only ------------------------------------------------
def test_supervisor_must_be_top_level():
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 2, "until": {"times": 1},
                       "body": [{"id": "sup", "kind": "supervisor", "prompt": "p"}]}]}
    with pytest.raises(WorkflowError):
        WorkflowSpec.parse(spec)


# --- safety: max_supervisions --------------------------------------------
def test_supervisor_max_supervisions_capped(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "adjust", "set_params": {"x": i}}
        return "ran"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "s1", "kind": "supervisor", "prefer": ["codex"], "prompt": "d1"},
        {"id": "s2", "kind": "supervisor", "needs": ["s1"], "prefer": ["codex"], "prompt": "d2"}]}
    with pytest.raises(WorkflowLimitError):
        _run(run_spec(runner, spec, limits=Limits(max_supervisions=1)))


# --- safety: injected step is bound by the same Limits --------------------
def test_supervisor_inject_privileged_sandbox_rejected(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "adjust", "inject": [
                {"id": "danger", "kind": "agent", "prefer": ["codex"],
                 "sandbox": "danger-full-access", "prompt": "rm -rf"}]}
        return "ran"

    runner, p = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "d"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert "danger" not in out["steps"]               # privileged inject dropped, never executed
    assert p._n == 1                                  # only the supervisor ran


# --- resume: re-applies injection deterministically -----------------------
def test_supervisor_resume_replays_injection(tmp_path):
    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            return {"action": "adjust", "inject": [
                {"id": "extra", "kind": "agent", "prefer": ["codex"], "prompt": "work"}]}
        return f"out-{i}"

    spec = {"steps": [{"id": "sup", "kind": "supervisor", "prefer": ["codex"], "prompt": "d"}]}

    p1 = ScriptedProvider("codex", responder)
    r1 = Runner("sup-resume", {"codex": p1, "gemini": p1, "claude": p1},
                {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    out1 = _run(run_spec(r1, spec))
    assert "extra" in out1["steps"] and p1._n > 0
    assert (tmp_path / "runs" / "sup-resume" / "wf-steps.json").exists()

    p2 = ScriptedProvider("codex", responder)
    r2 = Runner("sup-resume", {"codex": p2, "gemini": p2, "claude": p2},
                {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    out2 = _run(run_spec(r2, spec))
    assert p2._n == 0                                 # supervisor + injected step both journaled
    assert out2["steps"]["extra"] == out1["steps"]["extra"]   # identical plan on resume


# --- state: watched step values reach the coordinator prompt --------------
def test_supervisor_state_exposes_watched_steps(tmp_path):
    seen = {}

    def responder(prompt, schema, i):
        if _is_supervisor_call(schema):
            seen["prompt"] = prompt
            return {"action": "continue"}
        return {"verdict": "PASS"}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "rev", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
         "prompt": "review"},
        {"id": "sup", "kind": "supervisor", "needs": ["rev"], "prefer": ["codex"],
         "watch": ["rev"], "prompt": "verdict was {{supervisor.steps.rev.verdict}}"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert seen["prompt"] == "verdict was PASS"       # accumulated state templated into prompt


# --- recipe registration --------------------------------------------------
def test_adaptive_review_recipe_listed():
    assert "adaptive_review" in {r["name"] for r in list_recipes()}
    spec = get_recipe("adaptive_review")
    assert any(s["kind"] == "supervisor" for s in spec["steps"])


def test_adaptive_review_injects_audit_on_issues(tmp_path):
    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "action" in props:                         # the supervisor
            return {"action": "adjust", "inject": [
                {"id": "audit", "kind": "agent", "prefer": ["codex"],
                 "prompt": "adversarial audit of the flagged risk"}]}
        if set(props.get("verdict", {}).get("enum", [])) == {"DONE", "BLOCKED"}:
            return {"verdict": "DONE", "summary": "ok"}      # gate
        if "findings" in props:
            return {"verdict": "ISSUES", "severity": "high", "findings": ["off-by-one"]}
        return "audit-done"

    runner, _ = _scripted_runner(tmp_path, responder)
    out = _run(run_spec(runner, get_recipe("adaptive_review"),
                        {"topic": "feature", "subject_a": "A", "subject_b": "B"}))
    assert out["status"] == "DONE"
    assert out["output"]["audit"] == "audit-done"     # supervisor injected + ran the audit


# --- phase 2: the `when` deviation guard ----------------------------------
def test_supervisor_when_false_skips_coordinator(tmp_path):
    calls = {"sup": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "action" in props:
            calls["sup"] += 1
            return {"action": "adjust", "skip": ["after"]}
        if "verdict" in props:
            return {"verdict": "PASS"}
        return "after-ran"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "rev", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
         "prompt": "review"},
        {"id": "sup", "kind": "supervisor", "needs": ["rev"], "prefer": ["codex"],
         "when": {"any": [{"path": "steps.rev.value.verdict", "eq": "ISSUES"}]},
         "prompt": "decide"},
        {"id": "after", "kind": "agent", "needs": ["sup"], "prefer": ["codex"], "prompt": "go"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert calls["sup"] == 0                           # guard false → coordinator never fired
    assert out["steps"]["sup"]["skipped_guard"] is True
    assert out["steps"]["after"] == "after-ran"        # plan untouched, downstream ran


def test_supervisor_when_true_fires(tmp_path):
    calls = {"sup": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "action" in props:
            calls["sup"] += 1
            return {"action": "adjust", "skip": ["after"]}
        if "verdict" in props:
            return {"verdict": "ISSUES"}
        return "after-ran"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "rev", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
         "prompt": "review"},
        {"id": "sup", "kind": "supervisor", "needs": ["rev"], "prefer": ["codex"],
         "when": {"any": [{"path": "steps.rev.value.verdict", "eq": "ISSUES"}]},
         "prompt": "decide"},
        {"id": "after", "kind": "agent", "needs": ["sup"], "prefer": ["codex"], "prompt": "go"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert calls["sup"] == 1                           # guard true → coordinator fired
    assert "after" not in out["steps"]                 # and it skipped the future step


def test_when_select_over_list_and_numeric_threshold(tmp_path):
    fired = {"n": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "action" in props:
            fired["n"] += 1
            return {"action": "continue"}
        return {"findings": [{"severity": 7}, {"severity": 2}]}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "scan", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"findings": {"type": "array"}}},
         "prompt": "scan"},
        {"id": "sup", "kind": "supervisor", "needs": ["scan"], "prefer": ["codex"],
         # select drills into each list element; a leaf over a list fires on ANY match
         "when": {"any": [{"path": "steps.scan.value.findings", "select": "severity",
                           "gte": 5}]},
         "prompt": "decide"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert fired["n"] == 1                              # severity 7 >= 5 → fired


def test_when_not_combinator(tmp_path):
    fired = {"n": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "action" in props:
            fired["n"] += 1
            return {"action": "continue"}
        return {"verdict": "PASS"}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "g", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
         "prompt": "g"},
        {"id": "sup", "kind": "supervisor", "needs": ["g"], "prefer": ["codex"],
         "when": {"not": {"path": "steps.g.value.verdict", "eq": "PASS"}},
         "prompt": "decide"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert fired["n"] == 0                              # verdict==PASS → not(eq PASS)=false → skip


def test_bad_when_rejected_at_parse():
    bad = [
        {"steps": [{"id": "s", "kind": "supervisor", "prompt": "p",
                    "when": {"path": "x"}}]},                       # no operator
        {"steps": [{"id": "s", "kind": "supervisor", "prompt": "p",
                    "when": {"path": "x", "eq": 1, "ne": 2}}]},     # two operators
        {"steps": [{"id": "s", "kind": "supervisor", "prompt": "p",
                    "when": {"any": []}}]},                         # empty any
        {"steps": [{"id": "s", "kind": "supervisor", "prompt": "p",
                    "when": {"foo": 1}}]},                          # no path/combinator
    ]
    for spec in bad:
        with pytest.raises(WorkflowError):
            WorkflowSpec.parse(spec)


def test_adaptive_review_skips_supervisor_when_clean(tmp_path):
    sup_calls = {"n": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "action" in props:                          # the supervisor (should never fire)
            sup_calls["n"] += 1
            return {"action": "adjust", "inject": [
                {"id": "audit", "kind": "agent", "prefer": ["codex"], "prompt": "audit"}]}
        if set(props.get("verdict", {}).get("enum", [])) == {"DONE", "BLOCKED"}:
            return {"verdict": "DONE", "summary": "ok"}            # gate
        if "findings" in props:
            return {"verdict": "PASS", "severity": "low", "findings": []}   # clean reviews
        return "audit-done"

    runner, _ = _scripted_runner(tmp_path, responder)
    out = _run(run_spec(runner, get_recipe("adaptive_review"),
                        {"topic": "feature", "subject_a": "A", "subject_b": "B"}))
    assert out["status"] == "DONE"
    assert sup_calls["n"] == 0                          # clean path → coordinator never fired
    assert out["output"]["audit"] is None              # no audit injected
    assert out["output"]["supervision"]["skipped_guard"] is True

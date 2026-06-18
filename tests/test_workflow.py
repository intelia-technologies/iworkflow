"""Declarative workflow engine — deterministic tests (no quota, FakeProvider/Scripted)."""

import asyncio

import pytest

from iworkflow import (
    AgentResult, FakeProvider, Limits, Provider, Runner, WorkflowLimitError,
    get_recipe, list_recipes, run_spec,
)
from iworkflow.workflow import WorkflowError, WorkflowSpec, render


class ScriptedProvider(Provider):
    """Returns whatever `responder(prompt, schema, call_index)` decides.

    Lets a test drive verdicts/findings per call deterministically — the schema
    shapes FakeProvider can't produce (CONTINUE/STOP, findings arrays)."""

    def __init__(self, name, responder):
        super().__init__(name)
        self.responder = responder
        self._n = 0

    async def run(self, prompt, *, schema, sandbox="read-only", cwd=None, toolset=None, model=None):
        self.last_usage = {"input_tokens": 1, "output_tokens": 5, "cost_usd": None}
        i = self._n
        self._n += 1
        return self.responder(prompt, schema, i)


def _fake_runner(tmp_path, run_id="wf"):
    p = FakeProvider("codex")
    return Runner(run_id, {"codex": p}, {"codex": 4}, journal_dir=str(tmp_path)), p


def _scripted_runner(tmp_path, responder, run_id="wf"):
    # one provider object under three names → shared call sequence, any prefer routes to it
    p = ScriptedProvider("codex", responder)
    runner = Runner(run_id, {"codex": p, "gemini": p, "claude": p},
                    {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    return runner, p


def _run(coro):
    return asyncio.run(coro)


# --- templating -----------------------------------------------------------
def test_render_whole_value_keeps_type():
    ctx = {"params": {"xs": [1, 2, 3]}}
    assert render("{{params.xs}}", ctx) == [1, 2, 3]            # raw object
    assert render("n={{params.xs}}", ctx) == "n=[1, 2, 3]"      # stringified inline


def test_render_nested_path_and_missing():
    ctx = {"steps": {"g": {"value": {"verdict": "DONE"}}}}
    assert render("{{steps.g.value.verdict}}", ctx) == "DONE"
    assert render("{{steps.g.value.nope}}", ctx) is None


# --- basic steps ----------------------------------------------------------
def test_agent_step_templates_prompt_and_output(tmp_path):
    seen = {}

    def responder(prompt, schema, i):
        seen["prompt"] = prompt
        return f"answer:{prompt}"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "a", "kind": "agent", "prefer": ["codex"],
                       "prompt": "solve {{params.q}}"}],
            "output": "{{steps.a.value}}"}
    out = _run(run_spec(runner, spec, {"q": "X"}))
    assert seen["prompt"] == "solve X"
    assert out["status"] == "DONE"
    assert out["output"] == "answer:solve X"


def test_parallel_fans_out(tmp_path):
    runner, fake = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "fan", "kind": "parallel", "agents": [
        {"id": "a", "prefer": ["codex"], "prompt": "a {{params.q}}"},
        {"id": "b", "prefer": ["codex"], "prompt": "b {{params.q}}"}]}]}
    out = _run(run_spec(runner, spec, {"q": "Z"}))
    assert out["status"] == "DONE"
    assert [r["id"] for r in out["steps"]["fan"]] == ["a", "b"]
    assert fake._calls == 2


def test_pipeline_runs_each_item_through_stages(tmp_path):
    def responder(prompt, schema, i):
        return f"done:{prompt[-1]}"

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "p", "kind": "pipeline", "items": "{{params.items}}",
                       "stages": [{"id": "s1", "prefer": ["codex"], "prompt": "stage {{item}}"}]}]}
    out = _run(run_spec(runner, spec, {"items": ["1", "2", "3"]}))
    vals = [x["value"] for x in out["steps"]["p"]]
    assert vals == ["done:1", "done:2", "done:3"]


# --- gate / abort ---------------------------------------------------------
def test_gate_aborts_downstream(tmp_path):
    gate_schema = {"type": "object", "required": ["verdict"],
                   "properties": {"verdict": {"type": "string"}}}

    def responder(prompt, schema, i):
        return {"verdict": "BLOCKED"}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [
        {"id": "gate", "kind": "agent", "prefer": ["codex"], "schema": gate_schema,
         "gate": {"field": "verdict", "abort_on": "BLOCKED"}, "prompt": "assess"},
        {"id": "after", "kind": "agent", "needs": ["gate"], "prefer": ["codex"],
         "prompt": "should not run"}]}
    out = _run(run_spec(runner, spec))
    assert out["status"] == "ABORTED"
    assert out["aborted_at"] == "gate"
    assert "after" not in out["steps"]


# --- loops ----------------------------------------------------------------
def _findings_responder(unique):
    """find → findings (unique titles if `unique`, else constant → triggers dry)."""
    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "findings" in props:
            title = f"t{i}" if unique else "same"
            return {"findings": [{"title": title}]}
        return {"verdict": "CONTINUE"}
    return responder


def test_loop_times_runs_exactly_n(tmp_path):
    runner, fake = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 10,
                       "until": {"times": 3},
                       "body": [{"id": "b", "kind": "agent", "prefer": ["codex"],
                                 "prompt": "tick {{loop.iteration}}"}]}]}
    out = _run(run_spec(runner, spec))
    assert out["steps"]["L"] == []                  # no collect → empty accumulator
    assert fake._calls == 3                          # but the body ran exactly 3×


def test_loop_count_stops_at_target(tmp_path):
    runner, _ = _scripted_runner(tmp_path, _findings_responder(unique=True))
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 10,
                       "until": {"count": {"target": 3}},
                       "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
                       "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                                 "schema": {"type": "object",
                                            "properties": {"findings": {"type": "array"}}},
                                 "prompt": "find"}]}]}
    out = _run(run_spec(runner, spec))
    assert len(out["steps"]["L"]) >= 3


def test_loop_dry_stops_after_empty_rounds(tmp_path):
    runner, p = _scripted_runner(tmp_path, _findings_responder(unique=False))
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 10,
                       "until": {"dry": {"rounds": 2}},
                       "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
                       "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                                 "schema": {"type": "object",
                                            "properties": {"findings": {"type": "array"}}},
                                 "prompt": "find"}]}]}
    out = _run(run_spec(runner, spec))
    assert len(out["steps"]["L"]) == 1              # only the first round added anything
    assert p._n == 3                                 # round0 added, round1+2 dry → stop


def test_loop_budget_stops_on_tokens(tmp_path):
    # ScriptedProvider reports 5 output tokens/call → budget 12 stops after 3 calls
    runner, p = _scripted_runner(tmp_path, lambda pr, s, i: f"x{i}")
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 50,
                       "until": {"budget": {"output_tokens": 12}},
                       "body": [{"id": "b", "kind": "agent", "prefer": ["codex"],
                                 "prompt": "work"}]}]}
    _run(run_spec(runner, spec))
    assert p._n == 3


def test_loop_agent_decided(tmp_path):
    state = {"d": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "verdict" in props:                       # the decider
            state["d"] += 1
            return {"verdict": "STOP" if state["d"] >= 2 else "CONTINUE", "missing": []}
        return {"findings": [{"title": f"t{i}"}]}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 5,
                       "until": {"agent": {"prompt": "complete? {{loop.collected}}",
                                           "stop_when": "STOP", "prefer": ["codex"]}},
                       "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
                       "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                                 "schema": {"type": "object",
                                            "properties": {"findings": {"type": "array"}}},
                                 "prompt": "find {{loop.decision.missing}}"}]}]}
    out = _run(run_spec(runner, spec))
    assert state["d"] == 2                            # decider said STOP on round 2
    assert len(out["steps"]["L"]) == 2


def test_loop_vote_majority(tmp_path):
    rounds = {"votes": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "verdict" in props:                       # a voter
            rnd = rounds["votes"] // 3               # 3 voters per round
            rounds["votes"] += 1
            return {"verdict": "STOP" if rnd >= 1 else "CONTINUE"}
        return {"findings": [{"title": f"t{i}"}]}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 5,
                       "until": {"vote": {"prompt": "done?", "count": 3, "threshold": 2,
                                          "stop_when": "STOP", "prefer": ["codex"],
                                          "schema": {"type": "object",
                                                     "properties": {"verdict": {"type": "string"}}}}},
                       "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
                       "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                                 "schema": {"type": "object",
                                            "properties": {"findings": {"type": "array"}}},
                                 "prompt": "find"}]}]}
    out = _run(run_spec(runner, spec))
    assert len(out["steps"]["L"]) == 2               # round0 CONTINUE, round1 STOP


def test_loop_max_iterations_caps_runaway(tmp_path):
    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if "verdict" in props:
            return {"verdict": "CONTINUE"}           # never stops on its own
        return {"findings": [{"title": f"t{i}"}]}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 3,
                       "until": {"agent": {"prompt": "complete?", "stop_when": "STOP",
                                           "prefer": ["codex"]}},
                       "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
                       "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                                 "schema": {"type": "object",
                                            "properties": {"findings": {"type": "array"}}},
                                 "prompt": "find"}]}]}
    out = _run(run_spec(runner, spec))
    assert len(out["steps"]["L"]) == 3               # exactly max_iterations rounds


# --- spec validation ------------------------------------------------------
def test_parse_errors():
    bad_specs = [
        {"steps": []},                                              # empty
        {"steps": [{"id": "x", "kind": "bogus"}]},                  # bad kind
        {"steps": [{"id": "x", "kind": "agent"}]},                  # agent without prompt
        {"steps": [{"id": "L", "kind": "loop", "until": {"times": 1},
                    "body": [{"id": "b", "kind": "agent", "prompt": "p"}]}]},   # no max_iterations
        {"steps": [{"id": "L", "kind": "loop", "max_iterations": 2,
                    "until": {"count": {"target": 1}},              # count without collect
                    "body": [{"id": "b", "kind": "agent", "prompt": "p"}]}]},
    ]
    for bad in bad_specs:
        with pytest.raises(WorkflowError):
            WorkflowSpec.parse(bad)


# --- recipe registry ------------------------------------------------------
def test_builtin_recipes_listed():
    names = {r["name"] for r in list_recipes()}
    assert {"fan_synthesize", "review", "roadmap", "deep_review", "complex_security_audit"} <= names


def test_complex_security_audit_recipe_parses():
    spec = get_recipe("complex_security_audit")
    parsed = WorkflowSpec.parse(spec)
    assert parsed.name == "complex_security_audit"
    assert len(parsed.steps) == 5
def test_get_unknown_recipe_raises():
    with pytest.raises(KeyError):
        get_recipe("nope")


def test_fan_synthesize_recipe_runs(tmp_path):
    runner, fake = _fake_runner(tmp_path)
    out = _run(run_spec(runner, get_recipe("fan_synthesize"), {"goal": "ship it?"}))
    assert out["status"] == "DONE"
    assert out["output"] is not None
    assert fake._calls == 3                           # 2 proposers + 1 synth


def test_host_recipe_dir_discovered(tmp_path):
    import json

    rdir = tmp_path / "recipes"
    rdir.mkdir()
    (rdir / "custom.json").write_text(json.dumps({
        "name": "custom", "description": "host recipe",
        "steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "hi"}]}))
    names = {r["name"] for r in list_recipes(str(rdir))}
    assert "custom" in names


# --- safety policy / Limits (the council MUST-FIX set) --------------------
_GATE_SCHEMA = {"type": "object", "required": ["verdict"], "properties": {
    "verdict": {"type": "string", "enum": ["DONE", "BLOCKED"]}}}
_FINDINGS_SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}


def test_sandbox_passthrough_rejected_by_default(tmp_path):
    # the #1 council blocker: an untrusted spec must not pick a privileged sandbox
    runner, _ = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "a", "kind": "agent", "prefer": ["codex"],
                       "sandbox": "danger-full-access", "prompt": "rm -rf"}]}
    with pytest.raises(WorkflowLimitError):
        _run(run_spec(runner, spec))


def test_widened_limits_allow_sandbox(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "a", "kind": "agent", "prefer": ["codex"],
                       "sandbox": "workspace-write", "prompt": "go"}]}
    with pytest.raises(WorkflowLimitError):
        _run(run_spec(runner, spec))                              # default policy blocks
    widened = Limits(allowed_sandboxes=frozenset({"read-only", "workspace-write"}))
    out = _run(run_spec(runner, spec, limits=widened))
    assert out["status"] == "DONE"                                # trusted caller may opt in


def test_tools_injection_rejected_by_default(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "a", "kind": "agent", "prefer": ["codex"],
                       "tools": ["postgres"], "prompt": "go"}]}
    with pytest.raises(WorkflowLimitError):
        _run(run_spec(runner, spec))


def test_vote_threshold_zero_rejected():
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 3,
                       "until": {"vote": {"prompt": "?", "count": 3, "threshold": 0,
                                          "stop_when": "STOP"}},
                       "collect": {"from": "f", "dedup_by": "title"},
                       "body": [{"id": "f", "kind": "agent", "prompt": "p"}]}]}
    with pytest.raises(WorkflowError):
        WorkflowSpec.parse(spec)


def test_vote_threshold_above_count_rejected():
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 3,
                       "until": {"vote": {"prompt": "?", "count": 3, "threshold": 4,
                                          "stop_when": "STOP"}},
                       "collect": {"from": "f", "dedup_by": "title"},
                       "body": [{"id": "f", "kind": "agent", "prompt": "p"}]}]}
    with pytest.raises(WorkflowError):
        WorkflowSpec.parse(spec)


def test_parallel_width_capped():
    spec = {"steps": [{"id": "fan", "kind": "parallel", "agents": [
        {"id": f"a{i}", "prompt": "p"} for i in range(3)]}]}
    with pytest.raises(WorkflowLimitError):
        WorkflowSpec.parse(spec, Limits(max_parallel_width=2))


def test_loop_nesting_depth_capped():
    inner = {"id": "L3", "kind": "loop", "max_iterations": 2, "until": {"times": 1},
             "body": [{"id": "x", "kind": "agent", "prompt": "p"}]}
    mid = {"id": "L2", "kind": "loop", "max_iterations": 2, "until": {"times": 1},
           "body": [inner]}
    outer = {"id": "L1", "kind": "loop", "max_iterations": 2, "until": {"times": 1},
             "body": [mid]}
    with pytest.raises(WorkflowLimitError):
        WorkflowSpec.parse({"steps": [outer]}, Limits(max_loop_depth=2))


def test_max_iterations_above_policy_rejected():
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 9999,
                       "until": {"times": 1},
                       "body": [{"id": "x", "kind": "agent", "prompt": "p"}]}]}
    with pytest.raises(WorkflowLimitError):
        WorkflowSpec.parse(spec, Limits(max_loop_iterations=100))


def test_times_exceeding_max_iterations_rejected():
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 3,
                       "until": {"times": 10},
                       "body": [{"id": "x", "kind": "agent", "prompt": "p"}]}]}
    with pytest.raises(WorkflowError):
        WorkflowSpec.parse(spec)


def test_max_total_agent_calls_enforced(tmp_path):
    runner, fake = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 10,
                       "until": {"times": 10},
                       "body": [{"id": "b", "kind": "agent", "prefer": ["codex"],
                                 "prompt": "tick"}]}]}
    with pytest.raises(WorkflowLimitError):
        _run(run_spec(runner, spec, limits=Limits(max_total_agent_calls=3)))
    assert fake._calls == 3                                       # stopped exactly at the cap


def test_pipeline_items_capped(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {"steps": [{"id": "p", "kind": "pipeline", "items": "{{params.items}}",
                       "stages": [{"id": "s", "prefer": ["codex"], "prompt": "{{item}}"}]}]}
    with pytest.raises(WorkflowLimitError):
        _run(run_spec(runner, spec, {"items": [1, 2, 3]},
                      limits=Limits(max_pipeline_items=2)))


def test_no_stale_collect_on_abort(tmp_path):
    # body [gate, find], no dedup. gate aborts on iteration 1 BEFORE find reruns.
    # Buggy code (shared body_results + collect-before-abort) would re-harvest
    # iteration 0's stale find → 2 items. Fixed: fresh per iter + skip collect → 1.
    state = {"gate": 0}

    def responder(prompt, schema, i):
        props = (schema or {}).get("properties", {})
        if set(props.get("verdict", {}).get("enum", [])) == {"DONE", "BLOCKED"}:
            state["gate"] += 1
            return {"verdict": "BLOCKED" if state["gate"] >= 2 else "DONE"}
        return {"findings": [{"title": f"t{i}"}]}

    runner, _ = _scripted_runner(tmp_path, responder)
    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 5,
                       "until": {"times": 5},
                       "collect": {"from": "find", "path": "findings"},   # NO dedup
                       "body": [
                           {"id": "gate", "kind": "agent", "prefer": ["codex"],
                            "schema": _GATE_SCHEMA,
                            "gate": {"field": "verdict", "abort_on": "BLOCKED"},
                            "prompt": "g"},
                           {"id": "find", "kind": "agent", "prefer": ["codex"],
                            "schema": _FINDINGS_SCHEMA, "prompt": "f"}]}]}
    out = _run(run_spec(runner, spec))
    assert len(out["steps"]["L"]) == 1                            # no stale re-harvest


def test_resume_into_loop_is_deterministic(tmp_path):
    def make_responder():
        st = {"d": 0}

        def r(prompt, schema, i):
            if "verdict" in (schema or {}).get("properties", {}):
                st["d"] += 1
                return {"verdict": "STOP" if st["d"] >= 2 else "CONTINUE"}
            return {"findings": [{"title": f"t{i}"}]}
        return r

    spec = {"steps": [{"id": "L", "kind": "loop", "max_iterations": 5,
                       "until": {"agent": {"prompt": "done? {{loop.collected}}",
                                           "stop_when": "STOP", "prefer": ["codex"]}},
                       "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
                       "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                                 "schema": _FINDINGS_SCHEMA, "prompt": "find"}]}]}

    p1 = ScriptedProvider("codex", make_responder())
    r1 = Runner("resume-loop", {"codex": p1, "gemini": p1, "claude": p1},
                {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    out1 = _run(run_spec(r1, spec))
    assert len(out1["steps"]["L"]) == 2 and p1._n > 0

    p2 = ScriptedProvider("codex", make_responder())
    r2 = Runner("resume-loop", {"codex": p2, "gemini": p2, "claude": p2},
                {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    out2 = _run(run_spec(r2, spec))
    assert p2._n == 0                                            # fully cached, no new calls
    assert len(out2["steps"]["L"]) == 2                          # same accumulator on resume


def test_resume_journals_completed_steps(tmp_path):
    # a 2-step workflow (loop → summary): both top-level steps are journaled, so a
    # resume short-circuits the WHOLE loop (not just replays its body) with 0 agents.
    def responder(prompt, schema, i):
        if "verdict" in (schema or {}).get("properties", {}):
            return {"verdict": "STOP"}                           # loop stops after round 1
        if "findings" in (schema or {}).get("properties", {}):
            return {"findings": [{"title": f"t{i}"}]}
        return f"summary-{i}"

    spec = {"steps": [
        {"id": "scan", "kind": "loop", "max_iterations": 3,
         "until": {"agent": {"prompt": "done?", "stop_when": "STOP", "prefer": ["codex"]}},
         "collect": {"from": "find", "path": "findings", "dedup_by": "title"},
         "body": [{"id": "find", "kind": "agent", "prefer": ["codex"],
                   "schema": _FINDINGS_SCHEMA, "prompt": "find"}]},
        {"id": "sum", "kind": "agent", "needs": ["scan"], "prefer": ["codex"],
         "prompt": "summarize {{steps.scan.value}}"}]}

    p1 = ScriptedProvider("codex", responder)
    r1 = Runner("wf-steps-resume", {"codex": p1, "gemini": p1, "claude": p1},
                {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    out1 = _run(run_spec(r1, spec))
    assert (tmp_path / "runs" / "wf-steps-resume" / "wf-steps.json").exists()
    assert p1._n > 0

    p2 = ScriptedProvider("codex", responder)
    r2 = Runner("wf-steps-resume", {"codex": p2, "gemini": p2, "claude": p2},
                {"codex": 4, "gemini": 4, "claude": 4}, journal_dir=str(tmp_path))
    out2 = _run(run_spec(r2, spec))
    assert p2._n == 0                                            # both steps journaled → 0 agents
    assert out2["steps"]["sum"] == out1["steps"]["sum"]


def test_brainstorm_recipe_avoids_claude_interactive_hangs():
    spec = get_recipe("brainstorm")
    by_id = {step["id"]: step for step in spec["steps"]}

    for sid in ["phase1_search", "phase2_clarification", "phase4_proposals", "phase8_handoff"]:
        step = by_id[sid]
        assert step["prefer"][:2] == ["gemini", "codex"]
        assert step["prefer"] != ["claude:opus"]
        assert step["timeout_s"] <= 60
        assert step["heartbeat_interval_s"] <= 15

    assert spec["artifacts"] == [
        {"path": "openspec/changes/{{params.change_name}}/brainstorm.md", "type": "file"}
    ]

    for sid in ["phase6_write_spec", "phase7_update_wiki"]:
        step = by_id[sid]
        assert step["prefer"][:2] == ["codex", "gemini"]
        assert step["timeout_s"] <= 90
        assert step["heartbeat_interval_s"] <= 15

    decider = by_id["phase5_dialogue_loop"]["until"]["agent"]
    assert decider["prefer"][:2] == ["gemini", "codex"]
    assert decider["timeout_s"] <= 60
    assert decider["heartbeat_interval_s"] <= 15

    chat = by_id["phase5_dialogue_loop"]["body"][0]
    assert chat["prefer"][:2] == ["gemini", "codex"]
    assert chat["timeout_s"] <= 60
    assert chat["heartbeat_interval_s"] <= 15


def test_loop_decider_propagates_timeout_and_heartbeat(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    original_agent = runner.agent
    seen = {}

    async def recording_agent(prompt, *, label, schema=None, prefer=None, model=None, models=None,
                              role=None, sandbox="read-only", tools=None, auto_tools=None,
                              timeout_s=None, heartbeat_interval_s=None):
        if "#decide" in label:
            seen["timeout_s"] = timeout_s
            seen["heartbeat_interval_s"] = heartbeat_interval_s
            return AgentResult(label, "DONE", "codex", {"verdict": "STOP"})
        return await original_agent(
            prompt, label=label, schema=schema, prefer=prefer, model=model, models=models,
            role=role, sandbox=sandbox, tools=tools, auto_tools=auto_tools,
            timeout_s=timeout_s, heartbeat_interval_s=heartbeat_interval_s,
        )

    runner.agent = recording_agent
    spec = {"steps": [{
        "id": "L", "kind": "loop", "max_iterations": 2,
        "until": {"agent": {
            "prompt": "done?", "stop_when": "STOP", "prefer": ["codex"],
            "timeout_s": 17, "heartbeat_interval_s": 5,
        }},
        "body": [{"id": "work", "kind": "agent", "prefer": ["codex"], "prompt": "work"}],
    }]}

    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    assert seen == {"timeout_s": 17, "heartbeat_interval_s": 5}


def test_required_agent_exhaustion_fails_workflow(tmp_path):
    provider = FakeProvider("codex", limit_first_n=99)
    runner = Runner("required-exhausted", {"codex": provider}, {"codex": 1}, journal_dir=str(tmp_path))
    spec = {"steps": [{"id": "critical", "kind": "agent", "prefer": ["codex"], "prompt": "must work"}]}

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner, spec))

    assert "agent step 'critical' exhausted" in str(exc_info.value)


def test_optional_agent_exhaustion_can_continue(tmp_path):
    provider = FakeProvider("codex", limit_first_n=99)
    runner = Runner("optional-exhausted", {"codex": provider}, {"codex": 1}, journal_dir=str(tmp_path))
    spec = {"steps": [{
        "id": "best_effort", "kind": "agent", "prefer": ["codex"],
        "required": False, "prompt": "try",
    }]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["steps"]["best_effort"] is None


def test_required_artifact_missing_fails_workflow(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    runner.default_cwd = str(tmp_path)
    spec = {
        "artifacts": [{"path": "missing.txt", "type": "file"}],
        "steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "ok"}],
    }

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner, spec))

    assert "required workflow artifact(s) missing" in str(exc_info.value)
    assert str(tmp_path / "missing.txt") in str(exc_info.value)


def test_required_artifact_existing_allows_done(tmp_path):
    (tmp_path / "out.txt").write_text("ok", encoding="utf-8")
    runner, _ = _fake_runner(tmp_path)
    runner.default_cwd = str(tmp_path)
    spec = {
        "artifacts": [{"path": "out.txt", "type": "file"}],
        "steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "ok"}],
    }

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"


def test_preflight_checks_uncommitted_changes(tmp_path):
    import subprocess
    from iworkflow import run_spec, Runner, FakeProvider, Limits
    from iworkflow.workflow import WorkflowError
    
    # 1. Create a git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), check=True)
    
    # Create first commit so we can have status check
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "file.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=str(repo), check=True)
    
    # Now write an uncommitted change
    (repo / "file.txt").write_text("modified")
    
    # Create the runner pointing to this repo CWD
    codex = FakeProvider("codex")
    runner = Runner("preflight-test", {"codex": codex}, {"codex": 1}, journal_dir=str(tmp_path), default_cwd=str(repo))
    
    spec = {
        "name": "preflight_test",
        "execution": {
            "worktree": "new:branch-name"
        },
        "steps": [
            {"id": "s1", "kind": "agent", "prefer": ["codex"], "prompt": "hello"}
        ]
    }
    
    # Run should raise WorkflowError due to uncommitted changes
    with pytest.raises(WorkflowError) as exc_info:
        asyncio.run(run_spec(runner, spec, limits=Limits(allow_tools=True)))
    message = str(exc_info.value)
    assert "uncommitted changes" in message
    assert str(repo) in message

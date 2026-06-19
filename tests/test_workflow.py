"""Declarative workflow engine — deterministic tests (no quota, FakeProvider/Scripted)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from iworkflow import (
    AgentResult, FakeProvider, Limits, Provider, Runner, WorkflowLimitError,
    get_recipe, list_recipes, run_spec,
)
from iworkflow.workflow import WorkflowError, WorkflowSpec, _eval_when, render


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
        res = self.responder(prompt, schema, i)
        if asyncio.iscoroutine(res):
            res = await res
        return res


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


def test_command_step_emits_progress_events(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {
        "steps": [{
            "id": "cmd",
            "kind": "command",
            "command": "python3 -u -c \"print('one'); print('two')\"",
        }]
    }

    out = _run(run_spec(runner, spec, {}))

    assert out["status"] == "DONE"
    events_path = tmp_path / "runs" / "wf" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["dispatch", "output", "output", "done"]
    assert events[0]["label"] == "cmd"
    assert events[0]["provider"] == "local"
    assert events[1]["stream"] == "stdout"
    assert events[1]["text"] == "one\n"
    assert events[2]["text"] == "two\n"
    assert events[-1]["exit_code"] == 0


def test_checkpoint_parse_requires_schema_for_input_mode(tmp_path):
    valid = WorkflowSpec.parse({"steps": [{
        "id": "gate",
        "kind": "checkpoint",
        "mode": "input",
        "prompt": "Review decisions",
        "schema": {"type": "object", "required": ["approved"], "properties": {"approved": {}}},
        "output": str(tmp_path / "decision.json"),
    }]})
    assert valid.steps[0].kind == "checkpoint"

    with pytest.raises(WorkflowError, match="schema"):
        WorkflowSpec.parse({"steps": [{
            "id": "gate",
            "kind": "checkpoint",
            "mode": "input",
            "prompt": "Review decisions",
            "output": str(tmp_path / "decision.json"),
        }]})


def test_checkpoint_unattended_pauses_and_emits_pending_event(tmp_path):
    decision_path = tmp_path / "decision.json"
    runner, _ = _fake_runner(tmp_path, run_id="checkpoint-pause")
    spec = {"steps": [
        {"id": "gate", "kind": "checkpoint", "prompt": "Approve?", "output": str(decision_path)},
        {"id": "after", "kind": "command", "needs": ["gate"],
         "command": ["python3", "-c", "print('after')"]},
    ]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "PAUSED"
    assert out["pending_input"]["step_id"] == "gate"
    assert out["pending_input"]["output"] == str(decision_path)
    assert "after" not in out["steps"]
    events_path = tmp_path / "runs" / "checkpoint-pause" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "checkpoint_pending"
    assert events[-1]["label"] == "gate"


def test_checkpoint_resume_from_valid_output_file(tmp_path):
    decision_path = tmp_path / "decision.json"
    spec = {"steps": [
        {"id": "gate", "kind": "checkpoint", "mode": "input", "prompt": "Approve?",
         "schema": {"type": "object", "required": ["approved"],
                    "properties": {"approved": {}}},
         "output": str(decision_path)},
        {"id": "after", "kind": "command", "needs": ["gate"],
         "command": ["python3", "-c", "print('after')"]},
    ]}

    first_runner, _ = _fake_runner(tmp_path, run_id="checkpoint-resume")
    first = _run(run_spec(first_runner, spec))
    assert first["status"] == "PAUSED"

    decision_path.write_text(json.dumps({"approved": True}), encoding="utf-8")
    second_runner, _ = _fake_runner(tmp_path, run_id="checkpoint-resume")
    second = _run(run_spec(second_runner, spec))

    assert second["status"] == "DONE"
    assert second["steps"]["gate"] == {"approved": True}
    assert second["steps"]["after"]["exit_code"] == 0
    steps = json.loads((tmp_path / "runs" / "checkpoint-resume" / "wf-steps.json")
                       .read_text(encoding="utf-8"))
    assert steps["gate"]["value"] == {"approved": True}


def test_checkpoint_invalid_schema_stays_paused(tmp_path):
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps({"wrong": True}), encoding="utf-8")
    runner, _ = _fake_runner(tmp_path, run_id="checkpoint-invalid")
    spec = {"steps": [
        {"id": "gate", "kind": "checkpoint", "mode": "input", "prompt": "Approve?",
         "schema": {"type": "object", "required": ["approved"],
                    "additionalProperties": False, "properties": {"approved": {}}},
         "output": str(decision_path)},
        {"id": "after", "kind": "command", "needs": ["gate"],
         "command": ["python3", "-c", "print('after')"]},
    ]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "PAUSED"
    assert "validation_error" in out["pending_input"]
    assert "after" not in out["steps"]


def test_checkpoint_interactive_resolver_completes_inline(tmp_path):
    decision_path = tmp_path / "decision.json"
    seen = {}

    def resolver(request):
        seen.update(request)
        return {"approved": True}

    runner = Runner(
        "checkpoint-interactive",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
        checkpoint_resolver=resolver,
    )
    spec = {"steps": [
        {"id": "gate", "kind": "checkpoint", "mode": "input", "prompt": "Approve?",
         "schema": {"type": "object", "required": ["approved"],
                    "properties": {"approved": {}}},
         "output": str(decision_path)},
        {"id": "after", "kind": "command", "needs": ["gate"],
         "command": ["python3", "-c", "print('after')"]},
    ]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert seen["step_id"] == "gate"
    assert json.loads(decision_path.read_text(encoding="utf-8")) == {"approved": True}
    assert out["steps"]["after"]["exit_code"] == 0


def test_checkpoint_confirm_requires_explicit_affirmative(tmp_path):
    decision_path = tmp_path / "confirm.json"
    spec = {"steps": [
        {"id": "send_gate", "kind": "checkpoint", "mode": "confirm",
         "prompt": "Send email?", "output": str(decision_path)},
        {"id": "send", "kind": "command", "needs": ["send_gate"],
         "command": ["python3", "-c", "print('sent')"]},
    ]}

    negative_runner = Runner(
        "checkpoint-confirm",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
        checkpoint_resolver=lambda request: {"approved": False},
    )
    negative = _run(run_spec(negative_runner, spec))
    assert negative["status"] == "PAUSED"
    assert "send" not in negative["steps"]
    assert not decision_path.exists()

    positive_runner = Runner(
        "checkpoint-confirm",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path),
        checkpoint_resolver=lambda request: {"approved": True},
    )
    positive = _run(run_spec(positive_runner, spec))
    assert positive["status"] == "DONE"
    assert positive["steps"]["send_gate"] == {"approved": True}
    assert positive["steps"]["send"]["exit_code"] == 0


def test_top_level_when_parse_accepts_agent_and_leaves_missing_parallel_unchanged():
    when = {"path": "steps.gate.value.ok", "truthy": True}
    wf = WorkflowSpec.parse({"steps": [
        {"id": "gate", "kind": "agent", "prefer": ["codex"], "prompt": "gate"},
        {"id": "audit", "kind": "agent", "needs": ["gate"], "prefer": ["codex"],
         "when": when, "prompt": "audit"},
        {"id": "fan", "kind": "parallel", "needs": ["audit"], "agents": [
            {"id": "a", "prefer": ["codex"], "prompt": "a"},
        ]},
    ]})

    assert wf.steps[1].when == when
    assert wf.steps[2].when is None


def test_bad_when_on_top_level_steps_rejected_at_parse():
    bad_specs = [
        {"steps": [{
            "id": "cmd",
            "kind": "command",
            "command": ["true"],
            "when": {"path": "steps.x.value", "unknown_op": 1},
        }]},
        {"steps": [{
            "id": "agent",
            "kind": "agent",
            "prompt": "agent",
            "when": {"path": "steps.x.value", "unknown_op": 1},
        }]},
    ]

    for spec in bad_specs:
        with pytest.raises(WorkflowError) as exc_info:
            WorkflowSpec.parse(spec)
        message = str(exc_info.value)
        assert "operator" in message
        assert "eq" in message


def test_agent_when_executes_when_true_and_skips_when_false_without_provider(tmp_path):
    def responder(prompt, schema, i):
        if i == 0:
            return {"exit_code": 1}
        return "audit-ran"

    runner, provider = _scripted_runner(tmp_path, responder, run_id="conditional-agent-skip")
    spec = {"steps": [
        {"id": "build", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"exit_code": {"type": "integer"}}},
         "prompt": "build"},
        {"id": "audit", "kind": "agent", "needs": ["build"], "prefer": ["codex"],
         "when": {"path": "steps.build.value.exit_code", "eq": 0},
         "prompt": "audit"},
    ]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert provider._n == 1
    steps_path = tmp_path / "runs" / "conditional-agent-skip" / "wf-steps.json"
    steps = json.loads(steps_path.read_text(encoding="utf-8"))
    assert steps["audit"] == {"skipped": True, "ok": True, "kind": "agent"}
    events_path = tmp_path / "runs" / "conditional-agent-skip" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "skipped" and e["label"] == "audit" and e["kind"] == "agent"
               for e in events)

    def true_responder(prompt, schema, i):
        if i == 0:
            return {"exit_code": 0}
        return "audit-ran"

    true_runner, true_provider = _scripted_runner(
        tmp_path, true_responder, run_id="conditional-agent-run")
    true_out = _run(run_spec(true_runner, spec))
    assert true_out["status"] == "DONE"
    assert true_provider._n == 2
    assert true_out["steps"]["audit"] == "audit-ran"


def test_command_when_true_executes_and_false_skips(tmp_path):
    runner, _ = _fake_runner(tmp_path, run_id="conditional-command")
    spec = {"steps": [
        {"id": "tests_ok", "kind": "command", "command": ["python3", "-c", "raise SystemExit(0)"]},
        {"id": "deploy", "kind": "command", "needs": ["tests_ok"],
         "when": {"path": "steps.tests_ok.value.exit_code", "eq": 0},
         "command": ["python3", "-c", "print('deploy-ran')"]},
        {"id": "tests_fail", "kind": "command", "needs": ["deploy"],
         "command": ["python3", "-c", "raise SystemExit(1)"]},
        {"id": "publish", "kind": "command", "needs": ["tests_fail"],
         "when": {"path": "steps.tests_fail.value.exit_code", "eq": 0},
         "command": ["python3", "-c", "print('publish-ran')"]},
    ]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["steps"]["deploy"]["exit_code"] == 0
    assert "skipped" not in out["steps"]["deploy"]
    steps = json.loads((tmp_path / "runs" / "conditional-command" / "wf-steps.json")
                       .read_text(encoding="utf-8"))
    assert steps["publish"] == {"skipped": True, "ok": True, "kind": "command"}


def test_skipped_step_satisfies_needs_and_downstream_can_inspect_skip(tmp_path):
    seen = {}

    def responder(prompt, schema, i):
        seen[i] = prompt
        if i == 0:
            return {"ok": False}
        return f"ran:{prompt}"

    runner, provider = _scripted_runner(tmp_path, responder, run_id="conditional-needs")
    spec = {"steps": [
        {"id": "gate", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
         "prompt": "gate"},
        {"id": "audit", "kind": "agent", "needs": ["gate"], "prefer": ["codex"],
         "when": {"path": "steps.gate.value.ok", "truthy": True},
         "prompt": "audit"},
        {"id": "report", "kind": "agent", "needs": ["audit"], "prefer": ["codex"],
         "prompt": "report skipped={{steps.audit.skipped}} kind={{steps.audit.kind}}"},
        {"id": "notify", "kind": "agent", "needs": ["audit"], "prefer": ["codex"],
         "when": {"path": "steps.audit.skipped", "truthy": True},
         "prompt": "notify"},
    ]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert provider._n == 3
    assert seen[1] == "report skipped=true kind=agent"
    assert out["steps"]["report"] == "ran:report skipped=true kind=agent"
    assert out["steps"]["notify"] == "ran:notify"


def test_compound_when_over_multiple_steps_skips_when_false(tmp_path):
    def responder(prompt, schema, i):
        if "gate-a" in prompt:
            return {"verdict": "PASS"}
        if "gate-b" in prompt:
            return {"score": 0.9}
        return "route-ran"

    runner, provider = _scripted_runner(tmp_path, responder, run_id="conditional-compound")
    spec = {"steps": [
        {"id": "a", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
         "prompt": "gate-a"},
        {"id": "b", "kind": "agent", "prefer": ["codex"],
         "schema": {"type": "object", "properties": {"score": {"type": "number"}}},
         "prompt": "gate-b"},
        {"id": "route", "kind": "agent", "needs": ["a", "b"], "prefer": ["codex"],
         "when": {"all": [
             {"any": [
                 {"path": "steps.a.value.verdict", "eq": "ISSUES"},
                 {"path": "steps.b.value.score", "gte": 0.8},
             ]},
             {"path": "params.enabled", "truthy": True},
         ]},
         "prompt": "route"},
    ]}

    out = _run(run_spec(runner, spec, {"enabled": False}))

    assert out["status"] == "DONE"
    assert provider._n == 2
    steps = json.loads((tmp_path / "runs" / "conditional-compound" / "wf-steps.json")
                       .read_text(encoding="utf-8"))
    assert steps["route"] == {"skipped": True, "ok": True, "kind": "agent"}


def test_skipped_step_resume_uses_journal_without_reevaluating(tmp_path):
    spec = {"steps": [
        {"id": "lint", "kind": "agent", "prefer": ["codex"],
         "when": {"path": "params.run_lint", "truthy": True},
         "prompt": "lint"},
    ]}

    first_runner, first_provider = _scripted_runner(
        tmp_path, lambda prompt, schema, i: "lint-ran", run_id="conditional-resume")
    first = _run(run_spec(first_runner, spec, {"run_lint": False}))
    assert first["status"] == "DONE"
    assert first_provider._n == 0

    second_runner, second_provider = _scripted_runner(
        tmp_path, lambda prompt, schema, i: "lint-ran", run_id="conditional-resume")
    second = _run(run_spec(second_runner, spec, {"run_lint": False}))

    assert second["status"] == "DONE"
    assert second_provider._n == 0
    steps = json.loads((tmp_path / "runs" / "conditional-resume" / "wf-steps.json")
                       .read_text(encoding="utf-8"))
    assert steps["lint"] == {"skipped": True, "ok": True, "kind": "agent"}
    events = [json.loads(line) for line in (tmp_path / "runs" / "conditional-resume" / "events.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events].count("skipped") == 1


def test_eval_when_is_pure_and_deterministic():
    ctx = {"params": {}, "steps": {"a": {"value": {"score": 0.9}}}}
    when = {"path": "steps.a.value.score", "gte": 0.8}

    assert _eval_when(when, ctx) is True
    assert _eval_when(when, ctx) is True


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


def test_executor_teardown_tmux_on_success(tmp_path):
    runner, _ = _fake_runner(tmp_path, run_id="teardown-success")
    calls = []

    async def fake_teardown():
        calls.append("called")

    runner.teardown_tmux = fake_teardown
    spec = {"steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "ok"}]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert calls == ["called"]


def test_executor_teardown_tmux_on_workflow_error(tmp_path):
    provider = FakeProvider("codex", limit_first_n=99)
    runner = Runner(
        "teardown-error",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
    )
    calls = []

    async def fake_teardown():
        calls.append("called")

    runner.teardown_tmux = fake_teardown
    spec = {
        "steps": [{
            "id": "critical",
            "kind": "agent",
            "prefer": ["codex"],
            "prompt": "must work",
        }]
    }

    with pytest.raises(WorkflowError):
        _run(run_spec(runner, spec))

    assert calls == ["called"]


def test_executor_teardown_tmux_noop_without_claude_interactive(tmp_path):
    runner, _ = _fake_runner(tmp_path, run_id="teardown-no-claude")
    spec = {"steps": [{"id": "a", "kind": "agent", "prefer": ["codex"], "prompt": "ok"}]}

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"


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


def test_design_workflow_recipe_metadata():
    spec = get_recipe("design_workflow")
    assert spec["name"] == "design_workflow"
    assert spec["artifacts"] == [
        {"path": ".iworkflow/recipes/{{params.workflow_name}}.json", "type": "file"}
    ]
    by_id = {s["id"]: s for s in spec["steps"]}
    assert by_id["phase5_write_spec"]["write_paths"] == [
        ".iworkflow/recipes/{{params.workflow_name}}.json"
    ]


def test_brainstorm_recipe_avoids_claude_interactive_hangs():
    spec = get_recipe("brainstorm")
    by_id = {step["id"]: step for step in spec["steps"]}

    for sid in ["phase1_search", "phase2_clarification", "phase8_handoff"]:
        step = by_id[sid]
        assert step["prefer"][:2] == ["gemini", "codex"]
        assert step["prefer"] != ["claude:opus"]
        assert step["timeout_s"] <= 60
        assert step["heartbeat_interval_s"] <= 15

    phase4 = by_id["phase4_proposals"]
    assert phase4["prefer"][:2] == ["gemini", "codex"]
    assert phase4["timeout_s"] == 120
    assert phase4["heartbeat_interval_s"] <= 15

    phase3 = by_id["phase3_context"]
    for agent in phase3["agents"]:
        assert agent["prefer"][:2] == ["gemini", "codex"]
        assert "timeout_s" in agent
        assert "heartbeat_interval_s" in agent

    assert spec["artifacts"] == [
        {"path": "openspec/changes/{{params.change_name}}/brainstorm.md", "type": "file"}
    ]

    assert by_id["phase6_write_spec"]["write_paths"] == [
        "openspec/changes/{{params.change_name}}/brainstorm.md"
    ]
    assert by_id["phase7_update_wiki"]["write_paths"] == ["thoughts/shared/wiki/"]

    for sid in ["phase6_write_spec", "phase7_update_wiki"]:
        step = by_id[sid]
        assert step["prefer"][:2] == ["codex", "gemini"]
        assert step["timeout_s"] <= 90
        assert step["heartbeat_interval_s"] <= 15

    loop = by_id["phase5_dialogue_loop"]
    assert loop["max_iterations"] == 3
    assert loop["collect"] == {"from": "chat"}

    decider = loop["until"]["agent"]
    assert decider["prefer"][:2] == ["gemini", "codex"]
    assert decider["timeout_s"] == 120
    assert decider["heartbeat_interval_s"] == 30

    chat = loop["body"][0]
    assert chat["prefer"][:2] == ["gemini", "codex"]
    assert chat["timeout_s"] == 180
    assert chat["heartbeat_interval_s"] == 30
    assert "{{steps.phase4_proposals.value}}" in chat["prompt"]
    assert "{{params.user_input}}" in chat["prompt"]


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


def test_required_nested_agent_exhaustion_reports_full_label_and_timeout(tmp_path):
    provider = FakeProvider("codex", limit_first_n=99)
    runner = Runner("nested-exhausted", {"codex": provider}, {"codex": 1}, journal_dir=str(tmp_path))
    spec = {"steps": [{
        "id": "L", "kind": "loop", "max_iterations": 1,
        "until": {"times": 1},
        "body": [{
            "id": "chat", "kind": "agent", "prefer": ["codex"],
            "timeout_s": 17, "prompt": "must work",
        }],
    }]}

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner, spec))

    message = str(exc_info.value)
    assert "agent step 'L#0/chat' exhausted" in message
    assert "codex:RATE_LIMITED" in message
    assert "timeout_s=17" in message


def test_workflow_abort_kills_in_flight_provider_subprocesses(tmp_path):
    # A aborts via gate. B is a provider (agent step) running in flight.
    # B's subprocess and its grandchild should be killed by the runner's backstop.
    grandchild_pid_file = tmp_path / "grandchild.pid"
    cmd_script = (
        f"import subprocess, sys, time; "
        f"proc = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(10)\"]); "
        f"open(r'{grandchild_pid_file}', 'w').write(str(proc.pid)); "
        f"sys.stdout.flush(); "
        f"time.sleep(10)"
    )

    spec = {
        "steps": [
            {
                "id": "step_a",
                "kind": "agent",
                "prefer": ["codex"],
                "prompt": "abort",
                "schema": {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}},
                "gate": {"field": "verdict", "abort_on": "ABORT"}
            },
            {
                "id": "step_b",
                "kind": "agent",
                "prefer": ["codex"],
                "prompt": "run cmd",
            }
        ]
    }

    # Mock provider for step_a to sleep 0.3s and abort
    async def mock_a(prompt, schema, i):
        await asyncio.sleep(0.3)
        return {"verdict": "ABORT"}

    # Mock provider for step_b to run our long-running command script in a subprocess
    # (so it gets registered in the runner's active_pgids)
    async def mock_b(prompt, schema, i):
        provider = Provider("base")
        await provider._exec([sys.executable, "-c", cmd_script], "", cwd=str(tmp_path))
        return {"verdict": "DONE"}

    # We define a single ScriptedProvider whose responder routes based on the prompt
    async def dispatch_responder(prompt, schema, i):
        if "abort" in prompt:
            return await mock_a(prompt, schema, i)
        else:
            return await mock_b(prompt, schema, i)
    
    p_shared = ScriptedProvider("codex", dispatch_responder)
    runner = Runner("abort-kill-prov", {"codex": p_shared}, {"codex": 2}, journal_dir=str(tmp_path))

    out = _run(run_spec(runner, spec))
    assert out["status"] == "ABORTED"

    # Verify grandchild is killed by runner teardown
    import os
    import time
    time.sleep(0.5)
    assert grandchild_pid_file.exists()
    grandchild_pid = int(grandchild_pid_file.read_text())
    try:
        os.kill(grandchild_pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, f"grandchild process {grandchild_pid} survived workflow provider abort!"


def test_workflow_abort_kills_in_flight_subprocesses(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    
    # We create a temporary script that B executes. It spawns a grandchild and writes grandchild's PID to a file,
    # then sleeps forever.
    grandchild_pid_file = tmp_path / "grandchild.pid"
    cmd_script = (
        f"import subprocess, sys, time; "
        f"proc = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(10)\"]); "
        f"open(r'{grandchild_pid_file}', 'w').write(str(proc.pid)); "
        f"sys.stdout.flush(); "
        f"time.sleep(10)"
    )

    # A aborts via gate. B runs cmd_script.
    spec = {
        "steps": [
            {
                "id": "batch",
                "kind": "parallel",
                "agents": [
                    {
                        "id": "step_a",
                        "prompt": "always abort",
                        "schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
                        "gate": {"field": "verdict", "abort_on": "ABORT"},
                    },
                    {
                        "id": "step_b",
                        "prompt": "run cmd",
                        # We hijack B's prompt or mock it. Wait, step_b is an agent in parallel. 
                        # Wait! A parallel step can only contain agents. 
                        # But wait, we can just run two top-level steps in our new DAG executor!
                        # Since they have no dependencies, they will run concurrently as tasks!
                        # Step A is an agent that aborts via gate.
                        # Step B is a command step that executes our script.
                    }
                ]
            }
        ]
    }

    # Let us write the spec using two top-level steps (no needs) which run concurrently in a batch!
    spec = {
        "steps": [
            {
                "id": "step_a",
                "kind": "agent",
                "prefer": ["codex"],
                "prompt": "abort",
                "schema": {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}},
                "gate": {"field": "verdict", "abort_on": "ABORT"}
            },
            {
                "id": "step_b",
                "kind": "command",
                "command": [sys.executable, "-c", cmd_script]
            }
        ]
    }

    # Mock provider for step_a to return abort verdict
    async def mock_a(prompt, schema, i):
        await asyncio.sleep(0.3)
        return {"verdict": "ABORT"}
    p1 = ScriptedProvider("codex", mock_a)
    r = Runner("abort-kill", {"codex": p1}, {"codex": 2}, journal_dir=str(tmp_path))

    out = _run(run_spec(r, spec))
    assert out["status"] == "ABORTED"

    # Verify that step_b and its grandchild are killed
    import os
    import time
    time.sleep(0.5)
    assert grandchild_pid_file.exists()
    grandchild_pid = int(grandchild_pid_file.read_text())
    
    try:
        os.kill(grandchild_pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, f"grandchild process {grandchild_pid} survived workflow abort!"


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


def test_agent_schema_valid_value_marks_done_and_records_schema_ok(tmp_path):
    schema = {
        "type": "object",
        "required": ["verdict"],
        "properties": {"verdict": {"type": "string"}},
    }

    runner, _ = _scripted_runner(
        tmp_path,
        lambda prompt, schema, i: {"verdict": "PASS"},
        run_id="schema-valid",
    )
    spec = {
        "steps": [{
            "id": "check",
            "kind": "agent",
            "prefer": ["codex"],
            "schema": schema,
            "prompt": "check",
        }],
        "output": "{{steps.check.value.verdict}}",
    }

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["output"] == "PASS"
    assert out["steps"]["check"] == {"verdict": "PASS"}
    events = [
        json.loads(line)
        for line in (tmp_path / "runs" / "schema-valid" / "events.jsonl").read_text().splitlines()
    ]
    assert "schema_mismatch" not in {event["event"] for event in events}
    ledger = json.loads((tmp_path / "runs" / "schema-valid" / "ledger.jsonl").read_text())
    assert ledger["schema_ok"] is True


def test_required_agent_schema_mismatch_fails_workflow(tmp_path):
    schema = {"type": "object", "required": ["verdict"]}
    runner, _ = _scripted_runner(
        tmp_path,
        lambda prompt, schema, i: {"status": "ok"},
        run_id="schema-required",
    )
    spec = {
        "steps": [{
            "id": "critical",
            "kind": "agent",
            "prefer": ["codex"],
            "schema": schema,
            "prompt": "must validate",
        }]
    }

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner, spec))

    message = str(exc_info.value)
    assert "agent step 'critical' exhausted" in message
    assert "missing required key: 'verdict'" in message
    events = [
        json.loads(line)
        for line in (tmp_path / "runs" / "schema-required" / "events.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "schema_mismatch" for event in events)


def test_agent_schema_mismatch_fails_over_to_next_provider(tmp_path):
    schema = {"type": "object", "required": ["score"]}

    def responder(prompt, schema, i):
        return {"result": "ok"} if i == 0 else {"score": 0.9}

    runner, _ = _scripted_runner(tmp_path, responder, run_id="schema-failover")
    spec = {
        "steps": [{
            "id": "grade",
            "kind": "agent",
            "prefer": ["codex", "gemini"],
            "schema": schema,
            "prompt": "grade",
        }],
        "output": {
            "provider": "{{steps.grade.provider}}",
            "score": "{{steps.grade.value.score}}",
        },
    }

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["output"] == {"provider": "gemini", "score": 0.9}
    events = [
        json.loads(line)
        for line in (tmp_path / "runs" / "schema-failover" / "events.jsonl").read_text().splitlines()
    ]
    mismatches = [event for event in events if event["event"] == "schema_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["provider"] == "codex"
    assert mismatches[0]["why"] == "missing required key: 'score'"


def test_optional_agent_schema_mismatch_continues_with_null(tmp_path):
    schema = {"type": "object", "required": ["verdict"]}

    def responder(prompt, schema, i):
        return {"status": "ok"} if prompt == "try" else "continued"

    runner, _ = _scripted_runner(tmp_path, responder, run_id="schema-optional")
    spec = {
        "steps": [
            {
                "id": "best_effort",
                "kind": "agent",
                "prefer": ["codex"],
                "required": False,
                "schema": schema,
                "prompt": "try",
            },
            {
                "id": "after",
                "kind": "agent",
                "needs": ["best_effort"],
                "prefer": ["codex"],
                "prompt": "after",
            },
        ],
        "output": {
            "best_effort_ok": "{{steps.best_effort.ok}}",
            "after": "{{steps.after.value}}",
        },
    }

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["steps"]["best_effort"] is None
    assert out["output"] == {"best_effort_ok": False, "after": "continued"}


def test_agent_without_schema_does_not_validate_value(tmp_path):
    runner, _ = _scripted_runner(
        tmp_path,
        lambda prompt, schema, i: {"status": "ok"},
        run_id="schema-absent",
    )
    spec = {
        "steps": [{
            "id": "freeform",
            "kind": "agent",
            "prefer": ["codex"],
            "prompt": "no schema",
        }]
    }

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["steps"]["freeform"] == {"status": "ok"}
    events = [
        json.loads(line)
        for line in (tmp_path / "runs" / "schema-absent" / "events.jsonl").read_text().splitlines()
    ]
    assert "schema_mismatch" not in {event["event"] for event in events}
    ledger = json.loads((tmp_path / "runs" / "schema-absent" / "ledger.jsonl").read_text())
    assert ledger["schema_ok"] is None


def test_parallel_schema_mismatch_marks_subresult_failed(tmp_path):
    schema = {"type": "object", "required": ["verdict"]}

    def responder(prompt, schema, i):
        return {"verdict": "PASS"} if prompt == "agent a" else {}

    runner, _ = _scripted_runner(tmp_path, responder, run_id="schema-parallel")
    spec = {
        "steps": [{
            "id": "fan",
            "kind": "parallel",
            "agents": [
                {"id": "a", "prefer": ["codex"], "schema": schema, "prompt": "agent a"},
                {"id": "b", "prefer": ["codex"], "schema": schema, "prompt": "agent b"},
            ],
        }],
        "output": "{{steps.fan.ok}}",
    }

    out = _run(run_spec(runner, spec))

    assert out["status"] == "DONE"
    assert out["output"] is False
    assert out["steps"]["fan"][0]["ok"] is True
    assert out["steps"]["fan"][1]["ok"] is False
    assert out["steps"]["fan"][1]["value"] is None
    events = [
        json.loads(line)
        for line in (tmp_path / "runs" / "schema-parallel" / "events.jsonl").read_text().splitlines()
    ]
    mismatches = [event for event in events if event["event"] == "schema_mismatch"]
    assert [event["label"] for event in mismatches] == ["fan:b"]


def test_pipeline_schema_mismatch_marks_item_failed(tmp_path):
    schema = {"type": "object", "required": ["verdict"]}

    def responder(prompt, schema, i):
        return {"verdict": "PASS"} if prompt == "review good" else {}

    runner, _ = _scripted_runner(tmp_path, responder, run_id="schema-pipeline")
    spec = {
        "steps": [{
            "id": "pipe",
            "kind": "pipeline",
            "items": "{{params.items}}",
            "stages": [{
                "id": "review",
                "prefer": ["codex"],
                "schema": schema,
                "prompt": "review {{item}}",
            }],
        }],
        "output": "{{steps.pipe.ok}}",
    }

    out = _run(run_spec(runner, spec, {"items": ["good", "bad"]}))

    assert out["status"] == "DONE"
    assert out["output"] is False
    assert out["steps"]["pipe"][0]["ok"] is True
    assert out["steps"]["pipe"][1]["ok"] is False
    assert out["steps"]["pipe"][1]["value"] is None


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


def test_write_agent_fails_when_touching_unallowed_path(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    class WrongWriter(Provider):
        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            root = Path(cwd or repo)
            target = root / "docs" / "iworkflow-explainer.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("wrong", encoding="utf-8")
            return "wrote wrong file"

    runner = Runner(
        "wrong-write",
        {"codex": WrongWriter("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path / "journal"),
        default_cwd=str(repo),
    )
    spec = {"steps": [{
        "id": "write", "kind": "agent", "prefer": ["codex"],
        "sandbox": "write", "tools": ["write"],
        "write_paths": ["openspec/changes/x/brainstorm.md"],
        "prompt": "write",
    }]}

    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner, spec, limits=Limits(allow_tools=True, allowed_sandboxes=frozenset({"read-only", "write"}))))

    message = str(exc_info.value)
    assert "wrote outside allowed paths" in message
    assert "docs/iworkflow-explainer.html" in message
    assert "openspec/changes/x/brainstorm.md" in message


def test_write_agent_allows_declared_path(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    class CorrectWriter(Provider):
        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            root = Path(cwd or repo)
            target = root / "openspec" / "changes" / "x" / "brainstorm.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok", encoding="utf-8")
            return "wrote target"

    runner = Runner(
        "right-write",
        {"codex": CorrectWriter("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path / "journal"),
        default_cwd=str(repo),
    )
    spec = {"steps": [{
        "id": "write", "kind": "agent", "prefer": ["codex"],
        "sandbox": "write", "tools": ["write"],
        "write_paths": ["openspec/changes/x/brainstorm.md"],
        "prompt": "write",
    }]}

    out = _run(run_spec(runner, spec, limits=Limits(allow_tools=True, allowed_sandboxes=frozenset({"read-only", "write"}))))

    assert out["status"] == "DONE"


def test_write_paths_are_relative_to_workflow_cwd_inside_git_repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    work = repo / "work"
    work.mkdir()
    (work / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "work/.gitkeep"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    class SubdirWriter(Provider):
        async def run(self, prompt, *, schema=None, sandbox="read-only", cwd=None, toolset=None, model=None):
            root = Path(cwd or work)
            target = root / "openspec" / "changes" / "x" / "brainstorm.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok", encoding="utf-8")
            return "wrote target"

    runner = Runner(
        "subdir-write",
        {"codex": SubdirWriter("codex")},
        {"codex": 1},
        journal_dir=str(tmp_path / "journal"),
        default_cwd=str(work),
    )
    spec = {"steps": [{
        "id": "write", "kind": "agent", "prefer": ["codex"],
        "sandbox": "write", "tools": ["write"],
        "write_paths": ["openspec/changes/x/brainstorm.md"],
        "prompt": "write",
    }]}

    out = _run(run_spec(
        runner, spec,
        limits=Limits(allow_tools=True, allowed_sandboxes=frozenset({"read-only", "write"})),
    ))

    assert out["status"] == "DONE"


def test_command_step_executes_argv_successfully(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {
        "steps": [{
            "id": "run_echo",
            "kind": "command",
            "command": ["echo", "hello-world-argv"],
        }]
    }
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    cmd_res = out["steps"]["run_echo"]
    assert cmd_res["exit_code"] == 0
    assert "hello-world-argv" in cmd_res["stdout"]


def test_command_step_executes_shell_with_env_override(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {
        "steps": [{
            "id": "run_shell",
            "kind": "command",
            "command": "echo \"val=$MY_VAR\"",
            "env": {"MY_VAR": "custom-env-value"},
        }]
    }
    out = _run(run_spec(runner, spec))
    assert out["status"] == "DONE"
    cmd_res = out["steps"]["run_shell"]
    assert cmd_res["exit_code"] == 0
    assert "val=custom-env-value" in cmd_res["stdout"]


def test_command_step_gate_aborts_on_failure(tmp_path):
    runner, _ = _fake_runner(tmp_path)
    spec = {
        "steps": [{
            "id": "run_fail",
            "kind": "command",
            "command": ["false"],
            "gate": {"field": "exit_code", "abort_on": 1},
        }]
    }
    out = _run(run_spec(runner, spec))
    assert out["status"] == "ABORTED"
    assert out["aborted_at"] == "run_fail" 


def test_preflight_ignores_iworkflow_journal_dir(tmp_path):
    import subprocess
    from iworkflow import run_spec, Runner, FakeProvider, Limits

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Existing journal data enables resume but must not make the worktree dirty for preflight.
    journal = repo / ".iworkflow"
    old_run = journal / "runs" / "previous"
    old_run.mkdir(parents=True)
    (old_run / "events.jsonl").write_text("{}\n", encoding="utf-8")

    runner = Runner(
        "journal-ok",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(journal),
        default_cwd=str(repo),
    )
    spec = {
        "execution": {"worktree": "new:test", "git_clean_required": True},
        "steps": [{"id": "s", "kind": "agent", "prefer": ["codex"], "prompt": "ok"}],
    }

    out = _run(run_spec(runner, spec, limits=Limits(allow_tools=True)))
    assert out["status"] == "DONE"

    (repo / "dirty.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "dirty.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add dirty"], cwd=repo, check=True, capture_output=True)
    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    runner2 = Runner(
        "journal-dirty",
        {"codex": FakeProvider("codex")},
        {"codex": 1},
        journal_dir=str(journal),
        default_cwd=str(repo),
    )
    with pytest.raises(WorkflowError) as exc_info:
        _run(run_spec(runner2, spec, limits=Limits(allow_tools=True)))
    message = str(exc_info.value)
    assert "uncommitted changes" in message
    assert "dirty.txt" in message


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
            "worktree": "new:branch-name",
            "git_clean_required": True
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
    assert "file.txt" in message
    assert str(repo) in message

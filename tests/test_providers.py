import asyncio
import json
import sys

import pytest

from iworkflow.providers import (
    ClaudeInteractiveProvider,
    Provider,
    ProviderError,
    RateLimited,
    _find_schema_json,
    _response_text,
)


SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary"],
    "properties": {
        "verdict": {"enum": ["DONE"]},
        "summary": {},
    },
    "additionalProperties": False,
}


def test_classify_does_not_rate_limit_successful_output_mentions_limits():
    Provider._classify(0, "this answer discusses rate limit and quota handling")


def test_classify_failed_limit_text_raises_rate_limited():
    with pytest.raises(RateLimited):
        Provider._classify(1, "usage limit reached")


def test_classify_failed_non_limit_text_raises_provider_error():
    with pytest.raises(ProviderError, match="exit 1"):
        Provider._classify(1, "boom")


def test_classify_timeout_raises_provider_error():
    with pytest.raises(ProviderError, match="timed out"):
        Provider._classify(124, "usage limit reached")


def test_exec_kills_process_group_on_success(tmp_path):
    provider = Provider("base")

    # Parent spawns grandchild that sleeps for 10s, writes its PID to a file, then exits 0 instantly
    grandchild_pid_file = tmp_path / "grandchild.pid"
    code, stdout, stderr = asyncio.run(
        provider._exec(
            [
                sys.executable,
                "-u",
                "-c",
                f"import subprocess, sys; proc = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(10)\"]); open(r'{grandchild_pid_file}', 'w').write(str(proc.pid)); sys.stdout.flush()",
            ],
            "",
            cwd=str(tmp_path),
        )
    )

    assert code == 0
    assert stderr == ""
    assert grandchild_pid_file.exists()

    # Parse grandchild PID and verify it is dead
    grandchild_pid = int(grandchild_pid_file.read_text())
    import os
    import time
    time.sleep(0.2)
    try:
        os.kill(grandchild_pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, f"grandchild process {grandchild_pid} survived parent success exit!"


def test_exec_kills_process_group_on_timeout(tmp_path):
    provider = Provider("base")
    provider.timeout_s = 1

    # Spawn a grandchild that sleeps, print grandchild pid, then sleep
    code, stdout, stderr = asyncio.run(
        provider._exec(
            [
                sys.executable,
                "-u",
                "-c",
                "import subprocess, sys, time; proc = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(10)\"]); print(proc.pid); sys.stdout.flush(); time.sleep(10)",
            ],
            "",
            cwd=str(tmp_path),
        )
    )

    assert code == 124  # timeout
    assert "timeout" in stderr

    # Parse grandchild PID and verify it is dead
    grandchild_pid = int(stdout.strip())
    import os
    import time
    # wait briefly for signal propagation
    time.sleep(0.2)
    try:
        os.kill(grandchild_pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, f"grandchild process {grandchild_pid} survived timeout!"


def test_exec_runs_subprocess_in_cwd(tmp_path):
    provider = Provider("base")

    code, stdout, stderr = asyncio.run(
        provider._exec(
            [
                sys.executable,
                "-c",
                "import os; print(os.getcwd())",
            ],
            "",
            cwd=str(tmp_path),
        )
    )

    assert code == 0
    assert stderr == ""
    assert stdout.strip() == str(tmp_path)


def test_exec_emits_output_lines(tmp_path):
    provider = Provider("base")
    events = []

    code, stdout, stderr = asyncio.run(
        provider._exec(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys, time; print('alpha'); sys.stdout.flush(); time.sleep(0.05); print('omega')",
            ],
            "",
            cwd=str(tmp_path),
            on_event=lambda event, fields: events.append((event, fields)),
        )
    )

    assert code == 0
    assert stderr == ""
    assert stdout == "alpha\nomega\n"
    output_events = [fields["text"] for event, fields in events if event == "output"]
    assert output_events == ["alpha\n", "omega\n"]


def test_exec_does_not_split_long_jsonl_events(tmp_path):
    provider = Provider("base")
    events = []
    payload = {"type": "item.completed", "item": {"type": "agent_message", "text": "x" * 9000}}

    code, stdout, stderr = asyncio.run(
        provider._exec(
            [
                sys.executable,
                "-u",
                "-c",
                "import json; print(" + repr(json.dumps(payload)) + ")",
            ],
            "",
            cwd=str(tmp_path),
            on_event=lambda event, fields: events.append((event, fields)),
        )
    )

    assert code == 0
    assert stderr == ""
    assert stdout == json.dumps(payload) + "\n"
    output_events = [fields["text"] for event, fields in events if event == "output"]
    assert output_events == [json.dumps(payload) + "\n"]


def test_claude_interactive_passes_model_flag(tmp_path, monkeypatch):
    async def fast_sleep(_seconds):
        return None
    monkeypatch.setattr("iworkflow.providers.asyncio.sleep", fast_sleep)

    class RecordingClaudeInteractiveProvider(ClaudeInteractiveProvider):
        def __init__(self):
            super().__init__("claude", timeout_s=1, poll_s=0.01)
            self.commands = []
            self.pane_calls = 0

        async def _tmux(self, *args):
            self.commands.append(args)
            return ""

        async def _pane(self, session):
            self.pane_calls += 1
            if self.pane_calls <= 2:
                return "Claude Max plan mode"
            return "Claude Max plan mode\n⏺\n  response"

    provider = RecordingClaudeInteractiveProvider()

    result = asyncio.run(
        provider.run("say hello", schema=None, sandbox="read-only", model="opus")
    )

    new_session = next(command for command in provider.commands if command[0] == "new-session")
    assert result == "response"
    assert "--model opus" in new_session[-1]


def test_claude_interactive_starts_tmux_session_in_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "repo with spaces"
    cwd.mkdir()

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr("iworkflow.providers.asyncio.sleep", fast_sleep)

    class RecordingClaudeInteractiveProvider(ClaudeInteractiveProvider):
        def __init__(self):
            super().__init__("claude", timeout_s=1, poll_s=0.01)
            self.commands = []
            self.pane_calls = 0

        async def _tmux(self, *args):
            self.commands.append(args)
            return ""

        async def _pane(self, session):
            self.pane_calls += 1
            if self.pane_calls <= 2:
                return "Claude Max plan mode"
            return "Claude Max plan mode\n⏺\n  cwd response"

    provider = RecordingClaudeInteractiveProvider()

    result = asyncio.run(
        provider.run("say cwd", schema=None, sandbox="read-only", cwd=str(cwd))
    )

    new_session = next(command for command in provider.commands if command[0] == "new-session")
    assert result == "cwd response"
    assert "-c" in new_session and str(cwd) in new_session   # tmux native start-dir
    assert new_session[-1].startswith("claude ")


def test_claude_interactive_tmux_socket_prefix(monkeypatch):
    captured = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        captured.append(list(argv))
        return FakeProcess()

    monkeypatch.setattr(
        "iworkflow.providers.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    provider = ClaudeInteractiveProvider("claude", tmux_socket="iw_abc123")

    asyncio.run(provider._tmux("new-session", "-d", "-s", "iwf-99-1"))

    assert captured == [
        ["tmux", "-L", "iw_abc123", "new-session", "-d", "-s", "iwf-99-1"]
    ]


def test_claude_interactive_no_socket_no_prefix(monkeypatch):
    captured = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        captured.append(list(argv))
        return FakeProcess()

    monkeypatch.setattr(
        "iworkflow.providers.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    provider = ClaudeInteractiveProvider("claude")

    asyncio.run(provider._tmux("new-session", "-d", "-s", "iwf-99-1"))

    assert captured == [["tmux", "new-session", "-d", "-s", "iwf-99-1"]]
    assert "-L" not in captured[0]


def test_claude_interactive_run_uses_same_tmux_socket_for_every_command(monkeypatch):
    captured = []
    panes = [
        "Claude Max plan mode",
        "Claude Max plan mode",
        "Claude Max plan mode\n⏺\n  complete",
        "Claude Max plan mode\n⏺\n  complete",
        "Claude Max plan mode\n⏺\n  complete",
    ]

    async def fast_sleep(_seconds):
        return None

    class FakeProcess:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.returncode = 0

        async def communicate(self):
            return self.stdout.encode(), b""

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        captured.append(list(argv))
        if len(argv) > 3 and argv[1:3] == ("-L", "iw_run42"):
            subcommand = argv[3]
        else:
            subcommand = argv[1]
        if subcommand == "capture-pane":
            pane = panes.pop(0) if panes else "Claude Max plan mode\n⏺\n  complete"
            return FakeProcess(pane)
        return FakeProcess()

    monkeypatch.setattr("iworkflow.providers.asyncio.sleep", fast_sleep)
    monkeypatch.setattr(
        "iworkflow.providers.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    provider = ClaudeInteractiveProvider(
        "claude",
        timeout_s=1,
        poll_s=0.01,
        tmux_socket="iw_run42",
    )

    result = asyncio.run(provider.run("say hi", schema=None, sandbox="read-only"))

    assert result == "complete"
    assert all(argv[:3] == ["tmux", "-L", "iw_run42"] for argv in captured)
    subcommands = [argv[3] for argv in captured]
    assert {
        "kill-session",
        "new-session",
        "set-buffer",
        "paste-buffer",
        "send-keys",
        "capture-pane",
    } <= set(subcommands)


def test_find_schema_json_extracts_last_schema_valid_object():
    text = """
    noise {"verdict":"DONE","summary":"old"}
    later {"verdict":"BAD","summary":"wrong"}
    final {"verdict":"DONE","summary":"new"}
    trailing {"verdict":"DONE","summary":"extra","extra":true}
    """

    assert _find_schema_json(text, SCHEMA) == {
        "verdict": "DONE",
        "summary": "new",
    }


def test_response_text_strips_marker_and_tui_chrome():
    pane = """
    previous response
    ⏺
      Useful result
      ─────────────
      ❯
      plan mode on
      second line
    """

    assert _response_text(pane) == "Useful result\nsecond line"


def test_iter_json_objects_finds_nested_balanced_object():
    from iworkflow.providers import _iter_json_objects

    text = 'noise {"verdict":"DONE","summary":"x","meta":{"depth":2}} tail'
    assert list(_iter_json_objects(text)) == [{
        "verdict": "DONE",
        "summary": "x",
        "meta": {"depth": 2},
    }]


def test_response_text_rejects_plan_approval_chrome():
    pane = """
    ⏺
    Here is Claude's plan
    Would you like to proceed?
    shift+tab to approve
    """

    with pytest.raises(ProviderError, match="plan-approval chrome"):
        _response_text(pane)


def test_response_text_prefers_sentinel_markers():
    pane = """
    clutter
    <<<IWF>>>
    clean answer
    <<<END>>>
    """

    assert _response_text(pane) == "clean answer"



def test_resolve_cursor_model_aliases():
    from iworkflow.providers import _resolve_cursor_model

    assert _resolve_cursor_model(None) == "composer-2.5"
    assert _resolve_cursor_model("flash") == "composer-2.5-fast"
    assert _resolve_cursor_model("custom-model") == "custom-model"


def test_cursor_auth_required_detects_login_prompt():
    from iworkflow.providers import _cursor_auth_required

    assert _cursor_auth_required("Press any key to sign in\nCursor Agent")
    assert not _cursor_auth_required('{"type":"result","result":"ok"}')


def test_cursor_provider_run_returns_text(monkeypatch):
    from iworkflow.providers import CursorProvider

    provider = CursorProvider("cursor", model="composer-2.5", timeout_s=5)

    async def fake_exec(argv, stdin, cwd=None, on_event=None):
        assert argv[:4] == ["cursor-agent", "-p", "--model", "composer-2.5"]
        assert "--yolo" in argv
        assert "--trust" in argv
        assert "script" not in argv
        assert "--output-format" not in argv
        assert "Wrap your entire answer" in argv[-1]
        return 0, "<<<IWF>>>\nfrom cursor\n<<<END>>>", ""

    monkeypatch.setattr(provider, "_exec", fake_exec)
    result = asyncio.run(
        provider.run("say hi", schema=None, sandbox="read-only"),
    )
    assert result == "from cursor"
    assert provider.last_usage["model"] == "composer-2.5"


def test_cursor_provider_requires_login(monkeypatch):
    from iworkflow.providers import CursorProvider

    provider = CursorProvider("cursor", timeout_s=5)

    async def fake_exec(argv, stdin, cwd=None, on_event=None):
        return 1, "Press any key to sign in", "Cursor Agent"

    monkeypatch.setattr(provider, "_exec", fake_exec)
    with pytest.raises(ProviderError, match="not logged in"):
        asyncio.run(provider.run("say hi", schema=None))


def test_adaptive_polls_ramps_and_bounds():
    steps = list(ClaudeInteractiveProvider._adaptive_polls(0.5, 3.0, 10.0))
    assert steps[0] == 0.5
    assert steps[1] > steps[0]                 # ramps up
    assert all(s <= 3.0 + 1e-9 for s in steps)  # capped at max
    assert max(steps) == 3.0                    # reaches the ceiling
    assert abs(sum(steps) - 10.0) < 1e-6        # sums exactly to the total budget


def test_acquire_session_fresh_when_reuse_off():
    prov = ClaudeInteractiveProvider("claude", reuse_session=False)

    async def fake_tmux(*args):
        return ""

    async def fake_ready(session):
        return None

    prov._tmux = fake_tmux            # type: ignore[method-assign]
    prov._await_ready = fake_ready    # type: ignore[method-assign]

    s1, reused1 = asyncio.run(prov._acquire_session("k", None, None))
    s2, reused2 = asyncio.run(prov._acquire_session("k", None, None))
    assert reused1 is False and reused2 is False
    assert s1 != s2                   # a fresh session each call
    assert prov._sessions == {}       # nothing cached when reuse is off


def test_acquire_session_reuses_live_session():
    prov = ClaudeInteractiveProvider("claude", reuse_session=True)
    calls = []

    async def fake_tmux(*args):
        calls.append(args)
        return ""

    async def fake_ready(session):
        return None

    alive = {"v": False}

    async def fake_alive(session):
        return alive["v"]

    prov._tmux = fake_tmux            # type: ignore[method-assign]
    prov._await_ready = fake_ready    # type: ignore[method-assign]
    prov._session_alive = fake_alive  # type: ignore[method-assign]

    s1, reused1 = asyncio.run(prov._acquire_session("k", None, None))
    assert reused1 is False
    assert prov._sessions["k"] == s1

    alive["v"] = True                 # the warm session is now alive
    calls.clear()
    s2, reused2 = asyncio.run(prov._acquire_session("k", None, None))
    assert reused2 is True
    assert s2 == s1                   # same warm session reused
    flat = [" ".join(c) for c in calls]
    assert any("/clear" in f for f in flat)        # context wiped before reuse
    assert any("clear-history" in f for f in flat)  # scrollback wiped too


def test_acquire_session_recreates_dead_session():
    prov = ClaudeInteractiveProvider("claude", reuse_session=True)

    async def fake_tmux(*args):
        return ""

    async def fake_ready(session):
        return None

    async def fake_alive(session):
        return False                  # the cached session is dead

    prov._tmux = fake_tmux            # type: ignore[method-assign]
    prov._await_ready = fake_ready    # type: ignore[method-assign]
    prov._session_alive = fake_alive  # type: ignore[method-assign]

    s1, _ = asyncio.run(prov._acquire_session("k", None, None))
    s2, reused2 = asyncio.run(prov._acquire_session("k", None, None))
    assert reused2 is False           # dead cache entry → fresh session
    assert s2 != s1
    assert prov._sessions["k"] == s2

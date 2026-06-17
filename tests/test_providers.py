import asyncio
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

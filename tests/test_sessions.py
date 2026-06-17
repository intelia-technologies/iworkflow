"""Tests for subscription session probes (mocked — no real CLI calls)."""

import json

from iworkflow.sessions import (
    STATUS_MISSING,
    STATUS_NOT_LOGGED_IN,
    STATUS_READY,
    format_sessions_text,
    probe_claude,
    probe_codex,
    probe_cursor,
    probe_gemini,
    probe_sessions,
)


def test_probe_codex_logged_in(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[1:3] == ["login", "status"] and argv[0].endswith("codex")
        return 0, "Logged in using ChatGPT\n", ""

    monkeypatch.setattr("iworkflow.sessions.shutil.which", lambda x: "/usr/bin/codex")
    monkeypatch.setattr("iworkflow.sessions._run", fake_run)
    row = probe_codex()
    assert row.status == STATUS_READY
    assert row.logged_in is True
    assert row.auth_method == "chatgpt"


def test_probe_codex_not_logged_in(monkeypatch):
    monkeypatch.setattr("iworkflow.sessions.shutil.which", lambda x: "/usr/bin/codex")
    monkeypatch.setattr(
        "iworkflow.sessions._run",
        lambda argv, **kw: (1, "Not logged in\n", ""),
    )
    row = probe_codex()
    assert row.status == STATUS_NOT_LOGGED_IN
    assert row.fix_hint == "Run: codex login"


def test_probe_claude_json(monkeypatch):
    payload = {
        "loggedIn": True,
        "email": "a@b.com",
        "subscriptionType": "max",
        "authMethod": "claude.ai",
    }

    def fake_run(argv, **kwargs):
        return 0, json.dumps(payload), ""

    monkeypatch.setattr("iworkflow.sessions.shutil.which", lambda x: "/usr/bin/claude")
    monkeypatch.setattr("iworkflow.sessions._run", fake_run)
    row = probe_claude()
    assert row.status == STATUS_READY
    assert row.email == "a@b.com"
    assert row.subscription == "max"


def test_probe_gemini_models(monkeypatch):
    monkeypatch.setattr("iworkflow.sessions.shutil.which", lambda x: "/usr/bin/agy")
    monkeypatch.setattr(
        "iworkflow.sessions._run",
        lambda argv, **kw: (0, "Gemini 3.5 Flash\nClaude Opus\n", ""),
    )
    row = probe_gemini()
    assert row.status == STATUS_READY
    assert row.logged_in is True


def test_probe_cursor_not_logged_in(monkeypatch):
    monkeypatch.setattr("iworkflow.sessions.shutil.which", lambda x: "/usr/bin/cursor-agent")
    monkeypatch.setattr(
        "iworkflow.sessions._run",
        lambda argv, **kw: (0, "\n Not logged in\n\n", ""),
    )
    row = probe_cursor()
    assert row.status == STATUS_NOT_LOGGED_IN
    assert row.fix_hint == "Run: cursor-agent login"


def test_probe_missing_binary(monkeypatch):
    monkeypatch.setattr("iworkflow.sessions.shutil.which", lambda x: None)
    row = probe_codex()
    assert row.status == STATUS_MISSING


def test_probe_sessions_summary(monkeypatch):
    class P:
        def __init__(self, provider, logged_in, status, detail=""):
            self.provider = provider
            self.logged_in = logged_in
            self.status = status
            self.detail = detail

        def to_dict(self):
            return {
                "provider": self.provider,
                "status": self.status,
                "logged_in": self.logged_in,
                "detail": self.detail,
            }

    monkeypatch.setattr("iworkflow.sessions.probe_codex", lambda **kw: P("codex", True, STATUS_READY))
    monkeypatch.setattr(
        "iworkflow.sessions.probe_cursor",
        lambda **kw: P("cursor", False, STATUS_NOT_LOGGED_IN),
    )

    report = probe_sessions(["codex", "cursor"])
    assert report["ready"] == ["codex"]
    assert report["not_ready"] == ["cursor"]
    assert len(report["sessions"]) == 2


def test_format_sessions_text_includes_fix_hint():
    report = {
        "ready": [],
        "not_ready": ["cursor"],
        "unknown": [],
        "sessions": [{
            "provider": "cursor",
            "status": STATUS_NOT_LOGGED_IN,
            "detail": "Not logged in",
            "fix_hint": "Run: cursor-agent login",
        }],
    }
    text = format_sessions_text(report)
    assert "cursor-agent login" in text
    assert "✗" in text

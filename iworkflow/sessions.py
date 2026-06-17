"""Probe subscription CLI auth — which workers are logged in and ready.

Each check is fast and non-interactive (no model calls). Used by
`iworkflow sessions` and the MCP `iworkflow_sessions` tool before
dispatching work to codex / claude / gemini / cursor.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# --- status vocabulary -------------------------------------------------------
STATUS_READY = "ready"           # logged in / auth file present / models list
STATUS_NOT_LOGGED_IN = "not_logged_in"
STATUS_MISSING = "missing"       # binary not on PATH
STATUS_ERROR = "error"           # probe failed unexpectedly
STATUS_UNKNOWN = "unknown"       # could not determine (e.g. agy has no status cmd)


@dataclass
class SessionProbe:
    """Result of checking one subscription CLI."""

    provider: str
    label: str
    binary: str
    status: str
    logged_in: bool | None = None
    detail: str | None = None
    auth_method: str | None = None
    email: str | None = None
    subscription: str | None = None
    model: str | None = None
    version: str | None = None
    fix_hint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d.get("raw"):
            d.pop("raw", None)
        return d


def _run(
    argv: list[str],
    *,
    timeout_s: float = 12.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=merged,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "probe timed out"


def _resolve_binary(name: str, env_var: str | None = None) -> str:
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return override
    return shutil.which(name) or name


def _first_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def probe_codex(*, timeout_s: float = 12.0) -> SessionProbe:
    binary = _resolve_binary("codex")
    if not shutil.which(binary) and binary == "codex":
        return SessionProbe(
            provider="codex",
            label="Codex (ChatGPT)",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="`codex` not found on PATH",
            fix_hint="Install: npm install -g @openai/codex",
        )

    code, stdout, stderr = _run(
        [binary, "login", "status"],
        timeout_s=timeout_s,
    )
    combined = (stdout + "\n" + stderr).strip()
    low = combined.lower()

    if code == 127:
        return SessionProbe(
            provider="codex",
            label="Codex (ChatGPT)",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail=combined or "codex not found",
            fix_hint="Install: npm install -g @openai/codex",
        )

    if "logged in" in low and "not logged" not in low:
        method = None
        if "chatgpt" in low:
            method = "chatgpt"
        elif "api key" in low:
            method = "api_key"
        return SessionProbe(
            provider="codex",
            label="Codex (ChatGPT)",
            binary=binary,
            status=STATUS_READY,
            logged_in=True,
            detail=combined.splitlines()[0] if combined else "logged in",
            auth_method=method,
            fix_hint=None,
            raw={"exit_code": code},
        )

    if "not logged" in low or "no auth" in low or "login required" in low:
        return SessionProbe(
            provider="codex",
            label="Codex (ChatGPT)",
            binary=binary,
            status=STATUS_NOT_LOGGED_IN,
            logged_in=False,
            detail=combined.splitlines()[0] if combined else "not logged in",
            fix_hint="Run: codex login",
            raw={"exit_code": code},
        )

    # Fallback: codex doctor reports auth when configured
    dcode, dout, derr = _run([binary, "doctor", "--json"], timeout_s=timeout_s)
    doc = _first_json_object(dout) if dcode == 0 else None
    if doc:
        checks = doc.get("checks") or doc.get("sections") or {}
        auth = checks.get("auth") if isinstance(checks, dict) else None
        if isinstance(auth, dict):
            status_txt = str(auth.get("status", "")).lower()
            if status_txt in ("ok", "pass", "success") or "configured" in str(auth.get("detail", "")).lower():
                return SessionProbe(
                    provider="codex",
                    label="Codex (ChatGPT)",
                    binary=binary,
                    status=STATUS_READY,
                    logged_in=True,
                    detail="auth configured (via codex doctor)",
                    auth_method="chatgpt",
                    raw={"doctor": auth},
                )

    return SessionProbe(
        provider="codex",
        label="Codex (ChatGPT)",
        binary=binary,
        status=STATUS_UNKNOWN if code == 0 else STATUS_ERROR,
        logged_in=None,
        detail=combined[:300] if combined else (derr[:300] if derr else f"exit {code}"),
        fix_hint="Run: codex login  — or: codex doctor",
        raw={"exit_code": code},
    )


def probe_claude(*, timeout_s: float = 12.0) -> SessionProbe:
    binary = _resolve_binary("claude")
    if not shutil.which(binary) and binary == "claude":
        return SessionProbe(
            provider="claude",
            label="Claude Code",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="`claude` not found on PATH",
            fix_hint="Install Claude Code CLI",
        )

    code, stdout, stderr = _run(
        [binary, "auth", "status", "--json"],
        timeout_s=timeout_s,
    )
    combined = stdout + stderr
    if code == 127:
        return SessionProbe(
            provider="claude",
            label="Claude Code",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="claude not found",
        )

    data = _first_json_object(stdout)
    if data:
        logged = bool(data.get("loggedIn"))
        if logged:
            return SessionProbe(
                provider="claude",
                label="Claude Code",
                binary=binary,
                status=STATUS_READY,
                logged_in=True,
                detail=data.get("orgName") or data.get("email") or "logged in",
                auth_method=str(data.get("authMethod") or data.get("apiProvider") or ""),
                email=data.get("email"),
                subscription=data.get("subscriptionType"),
                raw=data,
            )
        return SessionProbe(
            provider="claude",
            label="Claude Code",
            binary=binary,
            status=STATUS_NOT_LOGGED_IN,
            logged_in=False,
            detail="not logged in",
            fix_hint="Run: claude auth login",
            raw=data,
        )

    low = combined.lower()
    if "logged in" in low and "not logged" not in low:
        return SessionProbe(
            provider="claude",
            label="Claude Code",
            binary=binary,
            status=STATUS_READY,
            logged_in=True,
            detail=combined.splitlines()[0][:200],
        )
    if "not logged" in low:
        return SessionProbe(
            provider="claude",
            label="Claude Code",
            binary=binary,
            status=STATUS_NOT_LOGGED_IN,
            logged_in=False,
            fix_hint="Run: claude auth login",
        )

    return SessionProbe(
        provider="claude",
        label="Claude Code",
        binary=binary,
        status=STATUS_ERROR if code != 0 else STATUS_UNKNOWN,
        logged_in=None,
        detail=combined[:300] or f"exit {code}",
        fix_hint="Run: claude auth login",
    )


def probe_gemini(*, timeout_s: float = 12.0) -> SessionProbe:
    """agy (Antigravity) — no dedicated auth status; `agy models` implies session."""
    binary = _resolve_binary("agy")
    if not shutil.which(binary) and binary == "agy":
        return SessionProbe(
            provider="gemini",
            label="Gemini (agy / Antigravity)",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="`agy` not found on PATH",
            fix_hint="Install Antigravity CLI (agy)",
        )

    code, stdout, stderr = _run([binary, "models"], timeout_s=timeout_s)
    combined = (stdout + "\n" + stderr).strip()
    low = combined.lower()

    if code == 127:
        return SessionProbe(
            provider="gemini",
            label="Gemini (agy / Antigravity)",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="agy not found",
        )

    auth_markers = (
        "not logged", "sign in", "login", "unauthorized", "authenticate",
        "no credentials", "auth required",
    )
    if any(m in low for m in auth_markers):
        return SessionProbe(
            provider="gemini",
            label="Gemini (agy / Antigravity)",
            binary=binary,
            status=STATUS_NOT_LOGGED_IN,
            logged_in=False,
            detail=combined.splitlines()[0][:200] if combined else "not authenticated",
            fix_hint="Sign in via Antigravity / agy install",
            raw={"exit_code": code},
        )

    # Successful models listing → treat as ready
    if code == 0 and stdout.strip():
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        sample = lines[0] if lines else "models available"
        return SessionProbe(
            provider="gemini",
            label="Gemini (agy / Antigravity)",
            binary=binary,
            status=STATUS_READY,
            logged_in=True,
            detail=f"{len(lines)} model(s); e.g. {sample[:60]}",
            model=sample if lines else None,
            raw={"model_count": len(lines)},
        )

    return SessionProbe(
        provider="gemini",
        label="Gemini (agy / Antigravity)",
        binary=binary,
        status=STATUS_UNKNOWN,
        logged_in=None,
        detail=combined[:300] if combined else f"exit {code}",
        fix_hint="Run: agy models  (if empty, sign in via Antigravity)",
        raw={"exit_code": code},
    )


def probe_cursor(*, timeout_s: float = 12.0) -> SessionProbe:
    binary = _resolve_binary("cursor-agent", "IWORKFLOW_CURSOR_BIN")
    if not shutil.which(binary) and binary == "cursor-agent":
        return SessionProbe(
            provider="cursor",
            label="Cursor Agent",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="`cursor-agent` not found on PATH",
            fix_hint="Install Cursor Agent CLI",
        )

    code, stdout, stderr = _run([binary, "status"], timeout_s=timeout_s)
    combined = (stdout + "\n" + stderr).strip()
    low = combined.lower()

    if code == 127:
        return SessionProbe(
            provider="cursor",
            label="Cursor Agent",
            binary=binary,
            status=STATUS_MISSING,
            logged_in=False,
            detail="cursor-agent not found",
        )

    if "not logged" in low:
        return SessionProbe(
            provider="cursor",
            label="Cursor Agent",
            binary=binary,
            status=STATUS_NOT_LOGGED_IN,
            logged_in=False,
            detail="Not logged in",
            fix_hint="Run: cursor-agent login",
            raw={"exit_code": code},
        )

    if "logged in" in low or "email" in low or "@" in combined:
        email_m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", combined)
        return SessionProbe(
            provider="cursor",
            label="Cursor Agent",
            binary=binary,
            status=STATUS_READY,
            logged_in=True,
            detail=combined.replace("\n", " ").strip()[:200],
            email=email_m.group(0) if email_m else None,
            raw={"exit_code": code},
        )

    # status with exit 0 but ambiguous output
    if code == 0 and combined and "press any key" not in low:
        return SessionProbe(
            provider="cursor",
            label="Cursor Agent",
            binary=binary,
            status=STATUS_READY,
            logged_in=True,
            detail=combined[:200],
        )

    return SessionProbe(
        provider="cursor",
        label="Cursor Agent",
        binary=binary,
        status=STATUS_NOT_LOGGED_IN if "sign in" in low else STATUS_UNKNOWN,
        logged_in=False if "sign in" in low else None,
        detail=combined[:300] if combined else f"exit {code}",
        fix_hint="Run: cursor-agent login",
        raw={"exit_code": code},
    )



_PROBE_FN_NAMES: dict[str, str] = {
    "codex": "probe_codex",
    "claude": "probe_claude",
    "gemini": "probe_gemini",
    "cursor": "probe_cursor",
}

_DEFAULT_ORDER = ("codex", "claude", "gemini", "cursor")


def probe_sessions(
    providers: list[str] | None = None,
    *,
    timeout_s: float = 12.0,
) -> dict[str, Any]:
    """Probe all (or selected) subscription CLIs. Returns summary + per-provider rows."""
    names = providers or list(_DEFAULT_ORDER)
    sessions: list[SessionProbe] = []
    for name in names:
        fn_name = _PROBE_FN_NAMES.get(name)
        fn = globals().get(fn_name) if fn_name else None
        if fn is None:
            sessions.append(SessionProbe(
                provider=name,
                label=name,
                binary=name,
                status=STATUS_UNKNOWN,
                logged_in=None,
                detail=f"unknown provider {name!r}",
            ))
            continue
        sessions.append(fn(timeout_s=timeout_s))

    ready = [s.provider for s in sessions if s.logged_in is True]
    not_ready = [s.provider for s in sessions if s.logged_in is False]
    unknown = [s.provider for s in sessions if s.logged_in is None]

    return {
        "ready": ready,
        "not_ready": not_ready,
        "unknown": unknown,
        "all_ready": len(not_ready) == 0 and len(unknown) == 0 and len(ready) > 0,
        "sessions": [s.to_dict() for s in sessions],
    }


def format_sessions_text(report: dict[str, Any]) -> str:
    """Human-readable table for terminal output."""
    lines = [
        "Subscription sessions (iworkflow workers):",
        f"  ready:     {', '.join(report['ready']) or '—'}",
        f"  not ready: {', '.join(report['not_ready']) or '—'}",
    ]
    if report.get("unknown"):
        lines.append(f"  unknown:   {', '.join(report['unknown'])}")
    lines.append("")
    for row in report["sessions"]:
        icon = {"ready": "✓", "not_logged_in": "✗", "missing": "?", "error": "!", "unknown": "~"}.get(
            row["status"], " ",
        )
        detail = row.get("detail") or row.get("status")
        fix = f"  → {row['fix_hint']}" if row.get("fix_hint") else ""
        lines.append(f"  {icon} {row['provider']:14} {detail}{fix}")
    return "\n".join(lines)

"""Provider adapters — each spawns a SUBSCRIPTION CLI as a worker.

The hard rule of this design: a worker is the CLI itself (`codex exec`,
`claude -p`, `agy -p`, `cursor-agent -p`), authenticated with the user's subscription. We never
call a paid provider API. Orchestration (scheduler.py) is deterministic Python,
so coordination spends zero tokens.

Structured output is provider-aware:
  - Codex  : native  `codex exec --output-schema FILE -o OUT`
  - Claude : native  `claude -p --output-format json --json-schema FILE`
  - Gemini : none    `agy -p` → parse a fenced/marker JSON block (fallback)
  - Cursor : none    `cursor-agent -p --output-format json` → JSON envelope / markers
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

from .minijsonschema import validate
from .toolsets import ToolSet


class RateLimited(Exception):
    """The provider's subscription hit a usage/session/rate limit."""


class ProviderError(Exception):
    """The CLI failed for a non-rate-limit reason (or timed out)."""


# Patterns that mean "this subscription is throttled" — re-dispatch elsewhere.
_LIMIT_PATTERNS = re.compile(
    r"(session limit|usage limit|rate limit|rate.?limited|quota|429|"
    r"too many requests|hit your .* limit|overloaded|resource[_ ]exhausted)",
    re.IGNORECASE,
)



IWF_BEGIN = "<<<IWF>>>"
IWF_END = "<<<END>>>"
_SENTINEL_INSTRUCTION = (
    f"\n\nWrap your entire answer between {IWF_BEGIN} and {IWF_END} "
    "on their own lines. Nothing before or after those markers."
)
_PLAN_CHROME = re.compile(
    r"would you like to proceed|shift\+tab to approve|here is claude'?s plan",
    re.IGNORECASE,
)


def _iter_json_objects(text: str):
    """Yield dict objects found via balanced-brace JSON scanning."""
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = end if end > i else i + 1


def _extract_sentinel(text: str) -> str | None:
    m = re.search(
        re.escape(IWF_BEGIN) + r"\s*(.*?)\s*" + re.escape(IWF_END),
        text,
        re.DOTALL,
    )
    return m.group(1).strip() if m else None


def _prompt_with_toolset(prompt: str, toolset: ToolSet | None) -> str:
    if toolset is None or toolset.is_empty():
        return prompt
    additions = toolset.prompt_additions()
    if not additions:
        return prompt
    return f"{additions}\n\n{prompt}"


def _toml_string(value: Any) -> str:
    return json.dumps(str(value))


def _toml_array(values: Any) -> str:
    if values is None:
        seq: list[Any] = []
    elif isinstance(values, (list, tuple)):
        seq = list(values)
    else:
        seq = [values]
    return json.dumps([str(value) for value in seq], separators=(",", ":"))


@dataclass
class Provider:
    """Base provider. Real adapters override `_argv` + output handling."""

    name: str
    model: str | None = None
    supports_schema: bool = True
    timeout_s: int = 300

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str,
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        raise NotImplementedError

    @staticmethod
    def _classify(exit_code: int, combined: str) -> None:
        # A SUCCESSFUL call (exit 0) is never a rate limit, even if its content
        # mentions "rate limit"/"quota"/"overloaded" — only a FAILED call can be
        # throttled. (Scanning successful output for those words was a false-
        # positive bug: a task ABOUT rate-limiting tripped its own detector.)
        if exit_code == 124:
            raise ProviderError("timed out")
        if exit_code == 0:
            return
        if _LIMIT_PATTERNS.search(combined):
            raise RateLimited(combined[-400:])
        raise ProviderError(f"exit {exit_code}: {combined[-400:]}")

    async def _exec(
        self,
        argv: list[str],
        stdin: str,
        cwd: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> tuple[int, str, str]:
        import os
        from pathlib import Path
        env = dict(os.environ)
        home = Path.home()
        extra_paths = [
            str(home / ".local" / "bin"),
            str(home / ".bun" / "bin"),
            str(home / ".antigravity" / "antigravity" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(extra_paths + [current_path] if current_path else extra_paths)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []

        async def feed_stdin() -> None:
            if proc.stdin is None:
                return
            if stdin:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
            proc.stdin.close()

        async def read_stream(stream: asyncio.StreamReader | None, name: str, parts: list[bytes]) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                parts.append(chunk)
                if on_event is not None:
                    on_event("output", {"stream": name, "text": chunk.decode(errors="replace")})

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    feed_stdin(),
                    read_stream(proc.stdout, "stdout", stdout_parts),
                    read_stream(proc.stderr, "stderr", stderr_parts),
                    proc.wait(),
                ),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, b"".join(stdout_parts).decode(errors="replace"), "".join([b"".join(stderr_parts).decode(errors="replace"), "timeout"])
        return proc.returncode, b"".join(stdout_parts).decode(errors="replace"), b"".join(stderr_parts).decode(errors="replace")

    async def _exec_observed(
        self,
        argv: list[str],
        stdin: str,
        cwd: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> tuple[int, str, str]:
        kwargs: dict[str, Any] = {"cwd": cwd}
        if on_event is not None and "on_event" in inspect.signature(self._exec).parameters:
            kwargs["on_event"] = on_event
        return await self._exec(argv, stdin, **kwargs)



def _parse_codex_usage(stdout: str) -> dict[str, Any] | None:
    """Pull token usage from a codex --json `turn.completed` event."""
    for line in stdout.splitlines():
        if '"usage"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            return {"input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"), "cost_usd": None}
    return None


class CodexProvider(Provider):
    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str = "read-only",
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        self.last_usage: dict[str, Any] | None = None
        schema_file = None
        fd, out_file = tempfile.mkstemp(suffix=".out")
        os.close(fd)
        try:
            full_prompt = _prompt_with_toolset(prompt, toolset)
            # --json puts events (incl. token usage) on stdout; -o writes the final
            # message to a file (so the answer survives the event stream).
            codex_sandbox = "workspace-write" if sandbox == "write" else sandbox
            argv = ["codex", "exec", "--sandbox", codex_sandbox, "--skip-git-repo-check",
                    "--color", "never", "--json", "-o", out_file]
            effective_model = model if model is not None else self.model
            if effective_model:
                argv += ["-m", effective_model]
            mcp_servers = (toolset.mcp_servers()
                           if toolset is not None and not toolset.is_empty() else {})
            if mcp_servers:
                argv += ["--ignore-user-config"]
                for name, cfg in mcp_servers.items():
                    prefix = f"mcp_servers.{name}"
                    if "url" in cfg:
                        argv += ["-c", f"{prefix}.url={_toml_string(cfg['url'])}"]
                    else:
                        if "command" in cfg:
                            argv += ["-c",
                                     f"{prefix}.command={_toml_string(cfg['command'])}"]
                        argv += ["-c", f"{prefix}.args={_toml_array(cfg.get('args', []))}"]
                # Codex exec has no clean per-call native-tool allow-list flag;
                # ToolSet.allowed() is intentionally not rendered here.
            if schema:
                fd, schema_file = tempfile.mkstemp(suffix=".schema.json")
                os.write(fd, json.dumps(schema).encode())
                os.close(fd)
                argv += ["--output-schema", schema_file]
            argv += ["-"]
            code, stdout, stderr = await self._exec_observed(argv, full_prompt, cwd=cwd, on_event=on_event)
            self._classify(code, stdout + "\n" + stderr)
            self.last_usage = _parse_codex_usage(stdout)
            with open(out_file, encoding="utf-8") as fh:
                answer = fh.read()
            if schema:
                payload = json.loads(answer or "{}")
                ok, why = validate(payload, schema)
                if not ok:
                    raise ProviderError(f"schema mismatch: {why}")
                return payload
            return answer.strip()
        finally:
            for f in (schema_file, out_file):
                if f and os.path.exists(f):
                    os.unlink(f)


class ClaudeProvider(Provider):
    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str = "read-only",
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        self.last_usage: dict[str, Any] | None = None
        schema_file = mcp_file = None
        try:
            full_prompt = _prompt_with_toolset(prompt, toolset)
            # Workers start LEAN: --strict-mcp-config skips the repo's heavy MCP
            # stack and --setting-sources user skips project settings/hooks, so a
            # worker spawns in ~4s instead of timing out loading the orchestrator's
            # context. The COMMON contract carries repo conventions inline anyway.
            argv = ["claude", "-p", "--output-format", "json",
                    "--strict-mcp-config", "--setting-sources", "user",
                    "--permission-mode", "plan"]  # plan = read-only-ish for the spike
            effective_model = model if model is not None else self.model
            if effective_model:
                argv += ["--model", effective_model]
            mcp_servers = (toolset.mcp_servers()
                           if toolset is not None and not toolset.is_empty() else {})
            if mcp_servers:
                fd, mcp_file = tempfile.mkstemp(suffix=".mcp.json")
                os.write(fd, json.dumps({"mcpServers": mcp_servers}).encode())
                os.close(fd)
                argv += ["--mcp-config", mcp_file]
            allowed = (toolset.allowed()
                       if toolset is not None and not toolset.is_empty() else [])
            if allowed:
                argv += ["--allowedTools", *allowed]
            if schema:
                argv += ["--json-schema", json.dumps(schema)]
            code, stdout, stderr = await self._exec_observed(argv, full_prompt, cwd=cwd, on_event=on_event)
            self._classify(code, stdout + "\n" + stderr)
            try:
                envelope = json.loads(stdout)            # claude -p --output-format json envelope
            except json.JSONDecodeError as e:
                raise ProviderError(f"Failed to parse Claude JSON output: {e}\nstdout: {stdout!r}\nstderr: {stderr!r}")
            usage = envelope.get("usage") if isinstance(envelope, dict) else None
            if isinstance(usage, dict):
                self.last_usage = {"input_tokens": usage.get("input_tokens"),
                                   "output_tokens": usage.get("output_tokens"),
                                   "cost_usd": envelope.get("total_cost_usd")}
            result = envelope.get("result", envelope)
            if schema:
                structured = envelope.get("structured_output") if isinstance(envelope, dict) else None
                payload = structured if structured is not None else (
                    result if isinstance(result, dict) else json.loads(result)
                )
                ok, why = validate(payload, schema)
                if not ok:
                    raise ProviderError(f"schema mismatch: {why}")
                return payload
            return result if isinstance(result, str) else json.dumps(result)
        finally:
            for f in (schema_file, mcp_file):
                if f and os.path.exists(f):
                    os.unlink(f)


class GeminiProvider(Provider):
    """agy has no schema flag → schema-less. Natural fit for the adversarial
    auditor and 1M-context sweeps. If a schema is requested, parse a JSON block."""

    supports_schema: bool = False

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str = "read-only",
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        argv = ["agy", "-p"]
        effective_model = model if model is not None else self.model
        if effective_model:
            argv += ["--model", effective_model]
        full = _prompt_with_toolset(prompt, toolset)
        if schema:
            full += ("\n\nReturn ONLY a JSON object matching this schema, "
                     "wrapped in ```json ... ```:\n" + json.dumps(schema))
        argv += [full]
        code, stdout, stderr = await self._exec_observed(argv, "", cwd=cwd, on_event=on_event)
        self._classify(code, stdout + "\n" + stderr)
        if not schema:
            return stdout.strip()
        m = re.search(r"```json\s*(.*?)\s*```", stdout, re.DOTALL)
        if m:
            payload = json.loads(m.group(1))
        else:
            payload = _find_schema_json(stdout, schema)
            if payload is None:
                raise ProviderError("no JSON block in agy output")
        ok, why = validate(payload, schema)
        if not ok:
            raise ProviderError(f"schema mismatch: {why}")
        return payload


CURSOR_MODEL_ALIASES: dict[str, str] = {
    "composer-2.5": "composer-2.5",
    "composer-2.5-fast": "composer-2.5-fast",
    "composer-2.5-flash": "composer-2.5-fast",  # legacy
    "composer": "composer-2.5",
    "flash": "composer-2.5-fast",
}
_CURSOR_AUTH_MARKERS = (
    "press any key to sign in",
    "not logged in",
    "cursor agent",
)


def _resolve_cursor_model(model: str | None, *, default: str = "composer-2.5") -> str:
    if not model:
        return default
    return CURSOR_MODEL_ALIASES.get(model, model)


def _cursor_auth_required(combined: str) -> bool:
    low = combined.lower()
    if "⏺" in combined or "<<<iwf>>>" in low:
        return False
    if any(marker in low for marker in _CURSOR_AUTH_MARKERS):
        return True
    if "sign in" in low and "cursor" in low:
        return True
    return False


@dataclass
class CursorProvider(Provider):
    """Drive Cursor Agent CLI (`cursor-agent`) on the user's Cursor subscription.

    Uses plain `--print` text mode (the default). `--output-format json` and
    wrapping in `script -q /dev/null` were both observed to hang cursor-agent in
    headless runs; the bare text invocation matches what works on a real TTY.
    """

    binary: str = field(
        default_factory=lambda: os.environ.get("IWORKFLOW_CURSOR_BIN", "cursor-agent"),
    )
    supports_schema: bool = False
    use_force: bool = True
    default_model: str = "composer-2.5"

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str = "read-only",
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        self.last_usage: dict[str, Any] | None = None
        effective = model if model is not None else self.model
        resolved = _resolve_cursor_model(effective, default=self.default_model)
        full = _prompt_with_toolset(prompt, toolset)
        if schema:
            full += (
                "\n\nReturn ONLY a JSON object matching this schema, "
                "wrapped in ```json ... ```:\n" + json.dumps(schema)
            )
        else:
            full += _SENTINEL_INSTRUCTION

        # Plain `--print` text mode (default). Adding `--output-format json` or
        # wrapping in `script -q /dev/null` made cursor-agent hang in headless
        # contexts; the bare invocation below matches what works on a real TTY.
        # `--yolo` skips tool-approval prompts, `--trust` skips the workspace
        # trust prompt — both required so a non-interactive run cannot block.
        argv = [self.binary, "-p", "--model", resolved]
        if self.use_force:
            argv.append("--yolo")
        argv.append("--trust")
        if cwd:
            argv.extend(["--workspace", cwd])
        argv.append(full)

        code, stdout, stderr = await self._exec_observed(argv, "", cwd=cwd, on_event=on_event)
        combined = stdout + "\n" + stderr
        if _cursor_auth_required(combined):
            raise ProviderError(
                "cursor-agent not logged in — run: cursor-agent login",
            )
        self._classify(code, combined)

        answer = stdout.strip()
        if not answer:
            raise ProviderError("cursor-agent returned empty output")
        self.last_usage = {
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "duration_ms": None,
            "model": resolved,
        }

        if schema:
            m = re.search(r"```json\s*(.*?)\s*```", answer, re.DOTALL)
            if m:
                payload = json.loads(m.group(1))
            else:
                payload = _find_schema_json(answer, schema)
                if payload is None:
                    raise ProviderError("no JSON block in cursor-agent output")
            ok, why = validate(payload, schema)
            if not ok:
                raise ProviderError(f"schema mismatch: {why}")
            return payload

        extracted = _extract_sentinel(answer)
        if extracted is not None:
            return extracted
        return answer


@dataclass
class FakeProvider(Provider):
    """Deterministic provider for testing the SCHEDULER without spending quota.

    `limit_first_n`: raise RateLimited on the first N calls (exercise failover).
    `delay_s`: simulate work so the per-provider semaphore is observable.
    Records `concurrent_peak` so a test can assert the cap was honored.
    """

    limit_first_n: int = 0
    delay_s: float = 0.0
    _calls: int = field(default=0, init=False)
    _active: int = field(default=0, init=False)
    concurrent_peak: int = field(default=0, init=False)

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str = "read-only",
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        self._calls += 1
        self.last_usage = {"input_tokens": 10, "output_tokens": 5, "cost_usd": None}
        if self._calls <= self.limit_first_n:
            raise RateLimited(f"{self.name}: simulated session limit (call {self._calls})")
        self._active += 1
        self.concurrent_peak = max(self.concurrent_peak, self._active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            payload = {"verdict": "DONE", "summary": f"{self.name} handled: {prompt[:40]}",
                       "provider": self.name}
            if schema:
                ok, why = validate(payload, schema)
                if not ok:
                    raise ProviderError(why)
            return payload
        finally:
            self._active -= 1


def _find_schema_json(text: str, schema: dict | None) -> Any:
    """Find the last JSON object in pane text that satisfies the schema."""
    found = None
    for obj in _iter_json_objects(text):
        if schema is None:
            found = obj
            continue
        ok, _ = validate(obj, schema)
        if ok:
            found = obj
    return found


@dataclass
class ClaudeInteractiveProvider(Provider):
    """Drive the INTERACTIVE `claude` TUI via tmux → stays on Pool 1
    (subscription), unlike `claude -p` which is Pool 2 (API-rate credit).

    No native schema → extracts a JSON object from the rendered pane (the
    marker fallback). Shares the one weekly subscription limit, so the scheduler
    must keep this provider's concurrency cap LOW (1-2) and treat it as opt-in.

    Mechanics proven live: ready in ~3s, response prefixed with `⏺`, completion
    by pane-stability + schema-valid JSON present.
    """

    supports_schema: bool = False
    permission_mode: str | None = None  # None = omit (answer mode); plan for write tasks
    poll_s: float = 3.0
    _seq: int = field(default=0, init=False)

    async def _tmux(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out.decode(errors="replace")

    async def _pane(self, session: str) -> str:
        return await self._tmux("capture-pane", "-p", "-S", "-3000", "-t", session)

    async def run(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        sandbox: str = "read-only",
        cwd: str | None = None,
        toolset: ToolSet | None = None,
        model: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Any:
        self._seq += 1
        session = f"iwf-{os.getpid()}-{self._seq}"
        full = _prompt_with_toolset(prompt, toolset)
        if schema:
            full += ("\n\nOutput ONLY one compact single-line JSON object matching "
                     "this schema — no code fences, no commentary:\n" + json.dumps(schema))
        else:
            full += _SENTINEL_INSTRUCTION
        await self._tmux("kill-session", "-t", session)
        try:
            command = "claude --strict-mcp-config --setting-sources user"
            if self.permission_mode:
                command += f" --permission-mode {self.permission_mode}"
            # tmux's native -c sets the session's start dir (robuster than `cd &&`)
            new_session = ["new-session", "-d", "-s", session, "-x", "300", "-y", "50"]
            if cwd:
                new_session += ["-c", cwd]
            await self._tmux(*new_session, command)

            # readiness
            for _ in range(int(40 / self.poll_s) + 1):
                p = await self._pane(session)
                if any(m in p for m in ("plan mode", "Claude Max", "for shortcuts")):
                    break
                await asyncio.sleep(self.poll_s)
            await asyncio.sleep(1.5)

            baseline = await self._pane(session)
            base_json = _find_schema_json(baseline, schema)

            # send (bracketed paste handles multi-line; Enter submits)
            await self._tmux("set-buffer", "--", full)
            await self._tmux("paste-buffer", "-p", "-t", session)
            await self._tmux("send-keys", "-t", session, "Enter")

            # completion: stable pane + (schema → fresh valid JSON) / (prose → ⏺ block)
            prev, stable = None, 0
            last_emitted = baseline
            max_polls = int(self.timeout_s / self.poll_s)
            for _ in range(max_polls):
                await asyncio.sleep(self.poll_s)
                cur = await self._pane(session)
                if on_event is not None and cur != last_emitted:
                    delta = cur[len(last_emitted):] if cur.startswith(last_emitted) else cur
                    on_event("output", {"stream": "tmux", "text": delta[-4096:]})
                    last_emitted = cur
                # tight Claude-specific limit banner only — NOT the generic regex,
                # which would match the echoed prompt or Claude discussing limits.
                if re.search(r"usage limit reached|approaching your usage limit",
                             cur, re.IGNORECASE):
                    raise RateLimited(cur[-400:])
                stable = stable + 1 if cur == prev else 0
                prev = cur
                if schema:
                    cand = _find_schema_json(cur, schema)
                    if cand is not None and cand != base_json and stable >= 1:
                        ok, why = validate(cand, schema)
                        if not ok:
                            raise ProviderError(f"schema mismatch: {why}")
                        return cand
                elif _extract_sentinel(cur) is not None and stable >= 1:
                    return _response_text(cur)
                elif "⏺" in cur and stable >= 2:          # prose response has settled
                    return _response_text(cur)
            raise ProviderError("timed out waiting for interactive response")
        finally:
            await self._tmux("kill-session", "-t", session)


def _response_text(pane: str) -> str:
    """Schema-less: sentinel-wrapped answer, else last `⏺` block minus TUI chrome."""
    extracted = _extract_sentinel(pane)
    if extracted is not None:
        if _PLAN_CHROME.search(extracted):
            raise ProviderError("interactive response is plan-approval chrome, not an answer")
        return extracted

    if "⏺" in pane:
        tail = pane.rsplit("⏺", 1)[1]
    else:
        tail = pane
    lines = []
    for ln in tail.splitlines():
        s = ln.strip()
        if not s or set(s) <= set("─—-") or s.startswith("❯") or "plan mode" in s:
            continue
        lines.append(s)
    result = "\n".join(lines).strip()
    if not result or _PLAN_CHROME.search(result):
        raise ProviderError("interactive response is plan-approval chrome, not an answer")
    return result

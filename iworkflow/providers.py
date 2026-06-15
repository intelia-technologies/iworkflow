"""Provider adapters — each spawns a SUBSCRIPTION CLI as a worker.

The hard rule of this design: a worker is the CLI itself (`codex exec`,
`claude -p`, `agy -p`), authenticated with the user's subscription. We never
call a paid provider API. Orchestration (scheduler.py) is deterministic Python,
so coordination spends zero tokens.

Structured output is provider-aware:
  - Codex  : native  `codex exec --output-schema FILE -o OUT`
  - Claude : native  `claude -p --output-format json --json-schema FILE`
  - Gemini : none    `agy -p` → parse a fenced/marker JSON block (fallback)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .minijsonschema import validate


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


@dataclass
class Provider:
    """Base provider. Real adapters override `_argv` + output handling."""

    name: str
    model: str | None = None
    supports_schema: bool = True
    timeout_s: int = 300

    async def run(self, prompt: str, *, schema: dict[str, Any] | None, sandbox: str) -> Any:
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

    async def _exec(self, argv: list[str], stdin: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode()), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", "timeout"
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class CodexProvider(Provider):
    async def run(self, prompt, *, schema, sandbox="read-only"):
        schema_file = out_file = None
        try:
            argv = ["codex", "exec", "--sandbox", sandbox,
                    "--skip-git-repo-check", "--color", "never"]
            if self.model:
                argv += ["-m", self.model]
            if schema:
                fd, schema_file = tempfile.mkstemp(suffix=".schema.json")
                os.write(fd, json.dumps(schema).encode()); os.close(fd)
                fd, out_file = tempfile.mkstemp(suffix=".out.json"); os.close(fd)
                argv += ["--output-schema", schema_file, "-o", out_file]
            argv += ["-"]
            code, stdout, stderr = await self._exec(argv, prompt)
            self._classify(code, stdout + "\n" + stderr)
            if schema:
                payload = json.loads(open(out_file).read() or "{}")
                ok, why = validate(payload, schema)
                if not ok:
                    raise ProviderError(f"schema mismatch: {why}")
                return payload
            return stdout.strip()
        finally:
            for f in (schema_file, out_file):
                if f and os.path.exists(f):
                    os.unlink(f)


class ClaudeProvider(Provider):
    async def run(self, prompt, *, schema, sandbox="read-only"):
        schema_file = None
        try:
            # Workers start LEAN: --strict-mcp-config skips the repo's heavy MCP
            # stack and --setting-sources user skips project settings/hooks, so a
            # worker spawns in ~4s instead of timing out loading the orchestrator's
            # context. The COMMON contract carries repo conventions inline anyway.
            argv = ["claude", "-p", "--output-format", "json",
                    "--strict-mcp-config", "--setting-sources", "user",
                    "--permission-mode", "plan"]  # plan = read-only-ish for the spike
            if self.model:
                argv += ["--model", self.model]
            if schema:
                fd, schema_file = tempfile.mkstemp(suffix=".schema.json")
                os.write(fd, json.dumps(schema).encode()); os.close(fd)
                argv += ["--json-schema", schema_file]
            code, stdout, stderr = await self._exec(argv, prompt)
            self._classify(code, stdout + "\n" + stderr)
            envelope = json.loads(stdout)            # claude -p --output-format json envelope
            result = envelope.get("result", envelope)
            if schema:
                payload = result if isinstance(result, dict) else json.loads(result)
                ok, why = validate(payload, schema)
                if not ok:
                    raise ProviderError(f"schema mismatch: {why}")
                return payload
            return result if isinstance(result, str) else json.dumps(result)
        finally:
            if schema_file and os.path.exists(schema_file):
                os.unlink(schema_file)


class GeminiProvider(Provider):
    """agy has no schema flag → schema-less. Natural fit for the adversarial
    auditor and 1M-context sweeps. If a schema is requested, parse a JSON block."""

    supports_schema: bool = False

    async def run(self, prompt, *, schema, sandbox="read-only"):
        argv = ["agy", "-p"]
        if self.model:
            argv += ["--model", self.model]
        full = prompt
        if schema:
            full += ("\n\nReturn ONLY a JSON object matching this schema, "
                     "wrapped in ```json ... ```:\n" + json.dumps(schema))
        argv += [full]
        code, stdout, stderr = await self._exec(argv, "")
        self._classify(code, stdout + "\n" + stderr)
        if not schema:
            return stdout.strip()
        m = re.search(r"```json\s*(\{.*?\})\s*```", stdout, re.DOTALL) or \
            re.search(r"(\{.*\})", stdout, re.DOTALL)
        if not m:
            raise ProviderError("no JSON block in agy output")
        payload = json.loads(m.group(1))
        ok, why = validate(payload, schema)
        if not ok:
            raise ProviderError(f"schema mismatch: {why}")
        return payload


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

    async def run(self, prompt, *, schema, sandbox="read-only"):
        self._calls += 1
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
    for m in reversed(re.findall(r"\{[^{}]*\}", text.replace("\n", " "))):
        try:
            obj = json.loads(m)
        except Exception:
            continue
        if schema is None:
            return obj
        ok, _ = validate(obj, schema)
        if ok:
            return obj
    return None


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
    permission_mode: str = "plan"     # read-only-safe; use acceptEdits for real work
    poll_s: float = 3.0
    _seq: int = field(default=0, init=False)

    async def _tmux(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out.decode(errors="replace")

    async def _pane(self, session: str) -> str:
        return await self._tmux("capture-pane", "-p", "-S", "-800", "-t", session)

    async def run(self, prompt, *, schema, sandbox="read-only"):
        self._seq += 1
        session = f"iwf-{os.getpid()}-{self._seq}"
        full = prompt
        if schema:
            full += ("\n\nOutput ONLY one compact single-line JSON object matching "
                     "this schema — no code fences, no commentary:\n" + json.dumps(schema))
        await self._tmux("kill-session", "-t", session)
        try:
            await self._tmux(
                "new-session", "-d", "-s", session, "-x", "300", "-y", "50",
                f"claude --strict-mcp-config --setting-sources user "
                f"--permission-mode {self.permission_mode}")

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
            max_polls = int(self.timeout_s / self.poll_s)
            for _ in range(max_polls):
                await asyncio.sleep(self.poll_s)
                cur = await self._pane(session)
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
                elif "⏺" in cur and stable >= 2:          # prose response has settled
                    return _response_text(cur)
            raise ProviderError("timed out waiting for interactive response")
        finally:
            await self._tmux("kill-session", "-t", session)


def _response_text(pane: str) -> str:
    """Schema-less: text of the last `⏺` response block, minus TUI chrome."""
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
    return "\n".join(lines).strip()

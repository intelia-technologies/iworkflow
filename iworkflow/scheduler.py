"""Deterministic, subscription-only, rate-limit-aware scheduler.

This is the differentiated piece — nothing in the OSS landscape (bernstein,
mcp-agent, agentapi, …) ships it: a runner that fans agents out across THREE
subscription CLIs with a per-provider concurrency cap, fails over when one
subscription is throttled, and resumes from a journal across rate-limit windows.

Zero API tokens (workers are the CLIs). Zero coordination tokens (this is code).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .providers import Provider, ProviderError, RateLimited


def log(msg: str) -> None:
    print(f"[iworkflow] {msg}", file=sys.stderr, flush=True)


@dataclass
class Attempt:
    provider: str
    outcome: str          # DONE | RATE_LIMITED | ERROR
    detail: str = ""


@dataclass
class AgentResult:
    label: str
    status: str           # DONE | EXHAUSTED  (EXHAUSTED = every provider failed)
    provider: str | None
    value: Any
    attempts: list[Attempt] = field(default_factory=list)
    resumed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "DONE"


# Billing tiers (as of 2026-06-15):
#   codex  (`codex exec`, ChatGPT login)  → SUBSCRIPTION  (included Codex usage)
#   gemini (`agy -p`, Google login)        → SUBSCRIPTION
#   claude (`claude -p` headless)          → POOL 2 / API-RATE CREDIT  (NOT subscription)
#   claude (interactive TUI via tmux/pty)  → POOL 1 / SUBSCRIPTION  [future backend]
#
# Default routes are SUBSCRIPTION-ONLY: workers go to Codex + Gemini. Claude's
# natural place is the interactive DRIVER (Pool 1 = the user's own session), not
# a metered headless worker. "claude" only appears in opt-in metered routes.
ROUTES = {
    "doer":    ["codex", "gemini"],           # structured doers: codex native schema
    "core":    ["codex", "gemini"],           # delicate cores stay on subscription
    "auditor": ["gemini", "codex"],           # schema-less role → agy fits perfectly
    "sweep":   ["gemini", "codex"],           # 1M-context sweeps → Gemini
    # opt-in, METERED (Pool 2) — only if the user explicitly accepts API-rate credit:
    "core_metered": ["codex", "claude"],
}


class Runner:
    def __init__(self, run_id: str, providers: dict[str, Provider],
                 caps: dict[str, int], journal_dir: str = ".iworkflow"):
        self.run_id = run_id
        self.providers = providers
        self.sems = {name: asyncio.Semaphore(caps.get(name, 2)) for name in providers}
        self.caps = caps
        self.journal_path = Path(journal_dir) / "runs" / run_id / "journal.jsonl"
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._done: dict[str, AgentResult] = self._load_journal()

    # --- resume -----------------------------------------------------------
    def _load_journal(self) -> dict[str, AgentResult]:
        done: dict[str, AgentResult] = {}
        if self.journal_path.exists():
            for line in self.journal_path.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec["status"] == "DONE":
                    done[rec["label"]] = AgentResult(
                        label=rec["label"], status="DONE", provider=rec["provider"],
                        value=rec["value"], resumed=True)
        if done:
            log(f"resume: {len(done)} agent(s) recovered from journal {self.journal_path}")
        return done

    def _persist(self, res: AgentResult) -> None:
        with self.journal_path.open("a") as fh:
            fh.write(json.dumps({"label": res.label, "status": res.status,
                                 "provider": res.provider, "value": res.value}) + "\n")

    # --- the agent() primitive -------------------------------------------
    async def agent(self, prompt: str, *, label: str, schema: dict | None = None,
                    role: str = "doer", prefer: list[str] | None = None) -> AgentResult:
        if label in self._done:
            log(f"RESUMED  {label}  (cached, 0 tokens)")
            return self._done[label]

        order = [p for p in (prefer or ROUTES.get(role, ["codex"])) if p in self.providers]
        attempts: list[Attempt] = []
        for name in order:
            prov = self.providers[name]
            use_schema = schema if prov.supports_schema or schema is None else schema
            async with self.sems[name]:
                log(f"DISPATCH {label} → {name} (cap {self.caps.get(name)})")
                try:
                    value = await prov.run(prompt, schema=use_schema, sandbox="read-only")
                    attempts.append(Attempt(name, "DONE"))
                    res = AgentResult(label, "DONE", name, value, attempts)
                    self._persist(res)
                    log(f"DONE     {label} ← {name}")
                    return res
                except RateLimited as e:
                    attempts.append(Attempt(name, "RATE_LIMITED", str(e)[:120]))
                    log(f"LIMITED  {label} ✗ {name} → failover")
                    continue
                except ProviderError as e:
                    attempts.append(Attempt(name, "ERROR", str(e)[:120]))
                    log(f"ERROR    {label} ✗ {name}: {str(e)[:80]} → failover")
                    continue
        res = AgentResult(label, "EXHAUSTED", None, None, attempts)
        self._persist(res)
        log(f"EXHAUSTED {label} — every subscription failed: "
            f"{[(a.provider, a.outcome) for a in attempts]}")
        return res

    # --- the parallel() barrier ------------------------------------------
    async def parallel(self, thunks: list[Callable[[], Awaitable[AgentResult]]]
                       ) -> list[AgentResult]:
        return list(await asyncio.gather(*(t() for t in thunks)))

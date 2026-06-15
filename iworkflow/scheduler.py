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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from .providers import Provider, ProviderError, RateLimited
from .routing import KIND_ROUTES as ROUTES  # re-exported for callers
from .routing import route


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


# Capability + scarcity aware default routing lives in routing.py (ROUTES is its
# KIND_ROUTES, re-exported above). Billing context: codex/gemini = subscription,
# claude interactive (tmux) = Pool-1 subscription, claude -p = Pool-2 (metered).


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
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # skip a partial line left by a crash mid-write
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
                    role: str | None = None, prefer: list[str] | None = None) -> AgentResult:
        if label in self._done:
            log(f"RESUMED  {label}  (cached, 0 tokens)")
            return replace(self._done[label], resumed=True)   # always flag cache hits

        if prefer:
            order, why = [p for p in prefer if p in self.providers], "explicit"
        else:
            order, why = route(role, schema=schema, prompt=prompt,
                               available=list(self.providers))
        log(f"ROUTE    {label}: {why} → {order}")
        attempts: list[Attempt] = []
        for name in order:
            prov = self.providers[name]
            # each provider handles its own schema capability (gemini parses a JSON
            # block; codex/claude use a native schema) — just pass it through.
            use_schema = schema
            async with self.sems[name]:
                log(f"DISPATCH {label} → {name} (cap {self.caps.get(name)})")
                try:
                    value = await prov.run(prompt, schema=use_schema, sandbox="read-only")
                    attempts.append(Attempt(name, "DONE"))
                    res = AgentResult(label, "DONE", name, value, attempts)
                    self._done[label] = res      # within-process dedup, not just cross-process
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

    # --- the pipeline() — per-item staged flow, NO barrier between stages -
    async def pipeline(self, items: list[Any],
                       *stages: Callable[[Any, Any, int], Awaitable[Any]]) -> list[Any]:
        """Run each item through all stages independently. Item A can be in
        stage 3 while item B is still in stage 1 (no barrier). Each stage is
        `await stage(prev_result, original_item, index)`; stage 1's prev is the
        item itself. A stage that raises drops that item to None. Concurrency is
        bounded naturally by the per-provider semaphores inside agent()."""
        async def run_item(item: Any, idx: int) -> Any:
            val = item
            for stage in stages:
                try:
                    val = await stage(val, item, idx)
                except Exception as e:        # noqa: BLE001 — drop item, keep batch
                    log(f"PIPELINE item {idx} dropped at a stage: {str(e)[:80]}")
                    return None
            return val
        return list(await asyncio.gather(
            *(run_item(it, i) for i, it in enumerate(items))))

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
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from .ledger import LedgerRecord, RunLedger, sha
from .learn import adjust_order
from .providers import Provider, ProviderError, RateLimited
from .routing import KIND_ROUTES as ROUTES  # noqa: F401 — re-exported as iworkflow.ROUTES
from .routing import route
from .stats import provider_stats
from .toolsets import ToolCatalog


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
    prompt_sha: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "DONE"


# Capability + scarcity aware default routing lives in routing.py (ROUTES is its
# KIND_ROUTES, re-exported above). Billing context: codex/gemini = subscription,
# claude interactive (tmux) = Pool-1 subscription, claude -p = Pool-2 (metered).


class Runner:
    def __init__(self, run_id: str, providers: dict[str, Provider],
                 caps: dict[str, int], journal_dir: str = ".iworkflow",
                 cooldown_s: float = 0.0, learn: bool = False,
                 catalog: ToolCatalog | None = None,
                 default_cwd: str | None = None):
        self.run_id = run_id
        self.providers = providers
        self.catalog = catalog
        self.default_cwd = default_cwd
        self.sems = {name: asyncio.Semaphore(caps.get(name, 2)) for name in providers}
        self.caps = caps
        self.cooldown_s = cooldown_s   # >0: skip a provider for this long after it throttles
        # learn=True: demote providers that have been failing across past ledgers
        self._stats = provider_stats(journal_dir) if learn else {}
        self.ledger = RunLedger(run_id, journal_dir)
        self._events_path = self.ledger.run_dir / "events.jsonl"
        self._done: dict[str, AgentResult] = self._load_done()

    def _emit(self, label: str, event: str, **fields: Any) -> None:
        """Append a structured telemetry event (the full execution trace)."""
        rec = {"ts": time.time(), "run_id": self.run_id, "label": label,
               "event": event, **fields}
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    # --- resume (durable ledger, see ledger.py) --------------------------
    def _load_done(self) -> dict[str, AgentResult]:
        done = {label: AgentResult(label=label, status="DONE", provider=rec.get("provider"),
                                   value=rec.get("value"), resumed=True,
                                   prompt_sha=rec.get("prompt_sha"))
                for label, rec in self.ledger.load_done().items()}
        if done:
            log(f"resume: {len(done)} agent(s) recovered from ledger {self.ledger.path}")
        return done

    def _record(self, res: AgentResult, *, prompt: str, schema: dict | None,
                attempts: list[Attempt], t_start: float,
                kind: str | None = None, tools: tuple[str, ...] = (),
                usage: dict[str, Any] | None = None) -> None:
        u = usage or {}
        self.ledger.append(LedgerRecord(
            run_id=self.run_id, label=res.label, status=res.status,
            provider=res.provider, value=res.value,
            attempts=[{"provider": a.provider, "outcome": a.outcome, "detail": a.detail}
                      for a in attempts],
            prompt_sha=sha(prompt), schema_sha=sha(schema) if schema else None,
            ts_start=t_start, ts_end=time.time(),
            error_class=(attempts[-1].outcome if res.status == "EXHAUSTED" and attempts
                         else None),
            retry_after=None, kind=kind, tools=list(tools),
            input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
            cost_usd=u.get("cost_usd")))

    # --- the agent() primitive -------------------------------------------
    async def agent(self, prompt: str, *, label: str, schema: dict | None = None,
                    role: str | None = None, prefer: list[str] | None = None,
                    sandbox: str = "read-only",
                    cwd: str | None = None,
                    tools: list[str] | None = None,
                    auto_tools: int | None = None) -> AgentResult:
        prompt_hash = sha(prompt)
        if label in self._done:
            cached = self._done[label]
            cached_sha = cached.prompt_sha
            if cached_sha is not None and cached_sha != prompt_hash:
                log(f"STALE    {label}  (prompt changed {cached_sha}→{prompt_hash}, re-run)")
                self._emit(label, "cache_invalidated",
                           old_sha=cached_sha, new_sha=prompt_hash)
                del self._done[label]
            else:
                log(f"RESUMED  {label}  (cached, 0 tokens)")
                self._emit(label, "resumed", provider=cached.provider)
                return replace(cached, resumed=True)   # always flag cache hits

        effective_cwd = cwd if cwd is not None else self.default_cwd
        t_start = time.time()
        # tool selection: explicit names/tags win; else auto-pick top-k by keyword
        # relevance to the prompt; else inject nothing (the lean default).
        if self.catalog and tools:
            toolset = self.catalog.resolve(tools)
        elif self.catalog and auto_tools:
            toolset = self.catalog.search(prompt, auto_tools)
        else:
            toolset = None
        tool_names = tuple(s.name for s in toolset.specs) if toolset else ()
        if prefer:
            order, why = [p for p in prefer if p in self.providers], "explicit"
        else:
            order, why = route(role, schema=schema, prompt=prompt,
                               available=list(self.providers))
            if self._stats:                              # empirical demotion
                adjusted = adjust_order(order, self._stats)
                if adjusted != order:
                    why, order = f"{why}→learned", adjusted
        log(f"ROUTE    {label}: {why} → {order}")
        self._emit(label, "route", kind=why, order=order, tools=list(tool_names))
        attempts: list[Attempt] = []
        for name in order:
            prov = self.providers[name]
            # throttle-aware: skip a provider still cooling down from a recent limit,
            # so we don't waste an attempt hammering a known-throttled subscription.
            if self.cooldown_s and self.ledger.is_cooling(name, time.time()):
                attempts.append(Attempt(name, "COOLING"))
                log(f"COOLING  {label} ⏳ {name} (skip) → next")
                self._emit(label, "cooling", provider=name)
                continue
            # each provider handles its own schema capability (gemini parses a JSON
            # block; codex/claude use a native schema) — just pass it through.
            use_schema = schema
            async with self.sems[name]:
                log(f"DISPATCH {label} → {name} (cap {self.caps.get(name)})")
                self._emit(label, "dispatch", provider=name)
                try:
                    value = await prov.run(prompt, schema=use_schema,
                                           sandbox=sandbox, cwd=effective_cwd, toolset=toolset)
                    # read usage immediately (no await between → race-free in asyncio)
                    usage = getattr(prov, "last_usage", None) or {}
                    attempts.append(Attempt(name, "DONE"))
                    res = AgentResult(label, "DONE", name, value, attempts, prompt_sha=prompt_hash)
                    self._done[label] = res      # within-process dedup, not just cross-process
                    self._record(res, prompt=prompt, schema=schema, attempts=attempts,
                                 t_start=t_start, kind=why, tools=tool_names, usage=usage)
                    self._emit(label, "done", provider=name,
                               ms=round((time.time() - t_start) * 1000),
                               input_tokens=usage.get("input_tokens"),
                               output_tokens=usage.get("output_tokens"),
                               cost_usd=usage.get("cost_usd"))
                    log(f"DONE     {label} ← {name}")
                    return res
                except RateLimited as e:
                    attempts.append(Attempt(name, "RATE_LIMITED", str(e)[:120]))
                    if self.cooldown_s:
                        self.ledger.record_cooldown(name, time.time() + self.cooldown_s)
                    log(f"LIMITED  {label} ✗ {name} → failover")
                    self._emit(label, "limited", provider=name)
                    continue
                except ProviderError as e:
                    attempts.append(Attempt(name, "ERROR", str(e)[:120]))
                    log(f"ERROR    {label} ✗ {name}: {str(e)[:80]} → failover")
                    self._emit(label, "error", provider=name, detail=str(e)[:120])
                    continue
        res = AgentResult(label, "EXHAUSTED", None, None, attempts)
        self._record(res, prompt=prompt, schema=schema, attempts=attempts,
                     t_start=t_start, kind=why, tools=tool_names)
        self._emit(label, "exhausted", attempts=[a.provider for a in attempts])
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

"""Deterministic, subscription-only, rate-limit-aware scheduler.

This is the differentiated piece — nothing in the OSS landscape (bernstein,
mcp-agent, agentapi, …) ships it: a runner that fans agents out across THREE
subscription CLIs with a per-provider concurrency cap, fails over when one
subscription is throttled, and resumes from a journal across rate-limit windows.

Zero API tokens (workers are the CLIs). Zero coordination tokens (this is code).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from .ledger import LedgerRecord, RunLedger, sha
from .learn import adjust_order
from .minijsonschema import validate
from .providers import ClaudeInteractiveProvider, Provider, ProviderError, RateLimited
from .routing import KIND_ROUTES as ROUTES  # noqa: F401 — re-exported as iworkflow.ROUTES
from .routing import route
from .stats import provider_stats
from .toolsets import ToolCatalog


def log(msg: str) -> None:
    print(f"[iworkflow] {msg}", file=sys.stderr, flush=True)


@dataclass
class Attempt:
    provider: str
    outcome: str          # DONE | RATE_LIMITED | ERROR | TIMEOUT | SCHEMA_MISMATCH
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
    timeout: bool = False
    last_heartbeat: float | None = None
    schema_ok: bool | None = None

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
        self._tmux_socket = f"iw_{run_id}"
        self._teardown_tmux_server = False
        for provider in self.providers.values():
            if isinstance(provider, ClaudeInteractiveProvider):
                provider.tmux_socket = self._tmux_socket
                self._teardown_tmux_server = True
        self.catalog = catalog
        self.default_cwd = default_cwd
        self.journal_dir = journal_dir
        self.sems = {name: asyncio.Semaphore(caps.get(name, 2)) for name in providers}
        self.caps = caps
        self.active_pgids = set()
        self.cooldown_s = cooldown_s   # >0: skip a provider for this long after it throttles
        # learn=True: demote providers that have been failing across past ledgers
        self._stats = provider_stats(journal_dir) if learn else {}
        self.ledger = RunLedger(run_id, journal_dir)
        self._events_path = self.ledger.run_dir / "events.jsonl"
        self._done: dict[str, AgentResult] = self._load_done()

    def _emit(self, label: str, event: str, **fields: Any) -> None:
        """Append a structured telemetry event (the full execution trace).

        `schema_mismatch` carries `label`, `provider`, and `why`.
        """
        rec = {"ts": time.time(), "run_id": self.run_id, "label": label,
               "event": event, **fields}
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    # --- resume (durable ledger, see ledger.py) --------------------------
    def _load_done(self) -> dict[str, AgentResult]:
        done = {label: AgentResult(label=label, status="DONE", provider=rec.get("provider"),
                                   value=rec.get("value"), resumed=True,
                                   prompt_sha=rec.get("prompt_sha"),
                                   timeout=rec.get("timeout", False),
                                   last_heartbeat=rec.get("last_heartbeat"),
                                   schema_ok=rec.get("schema_ok"))
                for label, rec in self.ledger.load_done().items()}
        if done:
            log(f"resume: {len(done)} agent(s) recovered from ledger {self.ledger.path}")
        return done

    def register_pgid(self, pgid: int) -> None:
        self.active_pgids.add(pgid)

    def unregister_pgid(self, pgid: int) -> None:
        self.active_pgids.discard(pgid)

    def kill_active_pgids(self) -> None:
        import os
        import signal
        for pgid in list(self.active_pgids):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                pass
            self.active_pgids.discard(pgid)

    async def teardown(self) -> None:
        await self.teardown_tmux()
        self.kill_active_pgids()

    async def teardown_tmux(self) -> None:
        if not self._teardown_tmux_server:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "-L",
                self._tmux_socket,
                "kill-server",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except OSError:
            return

    def _record(self, res: AgentResult, *, prompt: str, schema: dict | None,
                attempts: list[Attempt], t_start: float,
                kind: str | None = None, tools: tuple[str, ...] = (),
                usage: dict[str, Any] | None = None,
                model: str | None = None) -> None:
        u = usage or {}
        self.ledger.append(LedgerRecord(
            run_id=self.run_id, label=res.label, status=res.status,
            provider=res.provider, value=res.value,
            attempts=[{"provider": a.provider, "outcome": a.outcome, "detail": a.detail}
                      for a in attempts],
            prompt_sha=sha(prompt), schema_sha=sha(schema) if schema is not None else None,
            ts_start=t_start, ts_end=time.time(),
            error_class=(attempts[-1].outcome if res.status == "EXHAUSTED" and attempts
                         else None),
            retry_after=None, kind=kind, tools=list(tools),
            input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
            cost_usd=u.get("cost_usd"), model=model or u.get("model"),
            schema_ok=(None if schema is None else bool(res.schema_ok)),
            extra={"timeout": res.timeout, "last_heartbeat": res.last_heartbeat}))

    # --- the agent() primitive -------------------------------------------
    async def agent(self, prompt: str, *, label: str, schema: dict | None = None,
                    role: str | None = None,
                    prefer: list[str | dict[str, Any]] | None = None,
                    model: str | None = None,
                    models: dict[str, str] | None = None,
                    sandbox: str = "read-only",
                    cwd: str | None = None,
                    tools: list[str] | None = None,
                    auto_tools: int | None = None,
                    timeout_s: int | None = None,
                    heartbeat_interval_s: int | None = None) -> AgentResult:
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
        from .provider_models import format_prefer, parse_prefer_list

        if prefer:
            targets = [
                (p, m) for p, m in parse_prefer_list(
                    prefer, model=model, models=models)
                if p in self.providers
            ]
            why = "explicit"
        else:
            targets, why = route(role, schema=schema, prompt=prompt,
                                 available=list(self.providers))
            if self._stats:
                providers_only = [p for p, _ in targets]
                adjusted = adjust_order(providers_only, self._stats)
                if adjusted != providers_only:
                    by_prov = {p: m for p, m in targets}
                    targets = [(p, by_prov.get(p)) for p in adjusted]
                    why = f"{why}→learned"
        log(f"ROUTE    {label}: {why} → {format_prefer(targets)}")
        self._emit(label, "route", kind=why, order=format_prefer(targets),
                   tools=list(tool_names))
        display_prompt = prompt
        if toolset is not None:
            additions = toolset.prompt_additions()
            if additions:
                display_prompt = f"{additions}\n\n{prompt}"
        self._emit(
            label,
            "prompt",
            text=display_prompt,
            prompt_sha=prompt_hash,
            schema=bool(schema),
            tools=list(tool_names),
        )
        attempts: list[Attempt] = []
        last_heartbeat: float | None = None

        for name, target_model in targets:
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
                dispatch_model = target_model or (model if len(targets) == 1 else None)
                model_note = f" model={dispatch_model}" if dispatch_model else ""
                log(f"DISPATCH {label} → {name}{model_note} (cap {self.caps.get(name)})")
                self._emit(label, "dispatch", provider=name, model=dispatch_model)
                
                heartbeat_task = None
                if heartbeat_interval_s:
                    async def heartbeat_loop():
                        nonlocal last_heartbeat
                        while True:
                            await asyncio.sleep(heartbeat_interval_s)
                            last_heartbeat = time.time()
                            log(f"HEARTBEAT {label} ← {name}")
                            self._emit(label, "heartbeat", provider=name)
                    heartbeat_task = asyncio.create_task(heartbeat_loop())

                try:
                    def provider_event(event: str, fields: dict[str, Any]) -> None:
                        if event == "output":
                            text = str(fields.get("text", ""))
                            if text:
                                self._emit(
                                    label,
                                    "output",
                                    provider=name,
                                    stream=fields.get("stream", "stdout"),
                                    text=text,
                                )
                        elif event == "spawn":
                            pgid = fields.get("pgid")
                            if pgid:
                                self.register_pgid(pgid)
                        elif event == "reap":
                            pgid = fields.get("pgid")
                            if pgid:
                                self.unregister_pgid(pgid)

                    kwargs = {
                        "schema": use_schema,
                        "sandbox": sandbox,
                        "cwd": effective_cwd,
                        "toolset": toolset,
                        "model": dispatch_model,
                    }
                    if "on_event" in inspect.signature(prov.run).parameters:
                        kwargs["on_event"] = provider_event
                    coro = prov.run(prompt, **kwargs)
                    if timeout_s:
                        value = await asyncio.wait_for(coro, timeout=timeout_s)
                    else:
                        value = await coro
                    
                    if heartbeat_task:
                        heartbeat_task.cancel()

                    # read usage immediately (no await between → race-free in asyncio)
                    usage = getattr(prov, "last_usage", None) or {}
                    schema_ok = None
                    if schema is not None:
                        schema_ok, mismatch = validate(value, schema)
                        if not schema_ok:
                            attempts.append(Attempt(name, "SCHEMA_MISMATCH", mismatch[:120]))
                            log(f"SCHEMA  {label} ✗ {name}: {mismatch[:80]} → failover")
                            self._emit(label, "schema_mismatch", provider=name, why=mismatch)
                            continue
                    attempts.append(Attempt(name, "DONE"))
                    res = AgentResult(label, "DONE", name, value, attempts, 
                                      prompt_sha=prompt_hash, last_heartbeat=last_heartbeat,
                                      schema_ok=schema_ok)
                    self._done[label] = res      # within-process dedup, not just cross-process
                    self._record(res, prompt=prompt, schema=schema, attempts=attempts,
                                 t_start=t_start, kind=why, tools=tool_names, usage=usage,
                                 model=dispatch_model or usage.get("model"))
                    self._emit(label, "done", provider=name,
                               model=dispatch_model or usage.get("model"),
                               ms=round((time.time() - t_start) * 1000),
                               input_tokens=usage.get("input_tokens"),
                               output_tokens=usage.get("output_tokens"),
                               cost_usd=usage.get("cost_usd"))
                    log(f"DONE     {label} ← {name}")
                    return res
                except asyncio.TimeoutError:
                    if heartbeat_task:
                        heartbeat_task.cancel()
                    attempts.append(Attempt(name, "TIMEOUT", f"Exceeded {timeout_s}s"))
                    log(f"TIMEOUT  {label} ✗ {name} ({timeout_s}s) → failover")
                    self._emit(label, "timeout", provider=name, timeout_s=timeout_s)
                    continue
                except RateLimited as e:
                    if heartbeat_task:
                        heartbeat_task.cancel()
                    attempts.append(Attempt(name, "RATE_LIMITED", str(e)[:120]))
                    if self.cooldown_s:
                        self.ledger.record_cooldown(name, time.time() + self.cooldown_s)
                    log(f"LIMITED  {label} ✗ {name} → failover")
                    self._emit(label, "limited", provider=name)
                    continue
                except ProviderError as e:
                    if heartbeat_task:
                        heartbeat_task.cancel()
                    detail = str(e)
                    if schema is not None and detail.startswith("schema mismatch:"):
                        why_detail = detail.split("schema mismatch:", 1)[1].strip() or detail
                        attempts.append(Attempt(name, "SCHEMA_MISMATCH", why_detail[:120]))
                        log(f"SCHEMA  {label} ✗ {name}: {why_detail[:80]} → failover")
                        self._emit(label, "schema_mismatch", provider=name, why=why_detail)
                        continue
                    attempts.append(Attempt(name, "ERROR", detail[:120]))
                    log(f"ERROR    {label} ✗ {name}: {str(e)[:80]} → failover")
                    self._emit(label, "error", provider=name, detail=detail[:120])
                    continue
                finally:
                    if heartbeat_task and not heartbeat_task.done():
                        heartbeat_task.cancel()

        res = AgentResult(label, "EXHAUSTED", None, None, attempts, 
                          timeout=(attempts[-1].outcome == "TIMEOUT" if attempts else False),
                          last_heartbeat=last_heartbeat,
                          schema_ok=(False if schema is not None else None))
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

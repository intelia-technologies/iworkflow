"""Read run ledger(s) and report per-provider performance.

The data foundation for empirical routing (roadmap #5) and a quick "who's
winning" view: counts of DONE / RATE_LIMITED / ERROR / COOLING per provider,
plus success rate and average DONE latency. Pure reader — no core dependency,
no quota.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _ledger_files(journal_dir: str, run_id: str | None) -> list[Path]:
    base = Path(journal_dir) / "runs"
    if not base.exists():
        return []
    runs = [base / run_id] if run_id else sorted(base.iterdir())
    return [r / "ledger.jsonl" for r in runs if (r / "ledger.jsonl").exists()]


def provider_stats(journal_dir: str = ".iworkflow",
                   run_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Aggregate attempt outcomes + DONE latency per provider across ledger(s)."""
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"DONE": 0, "RATE_LIMITED": 0, "ERROR": 0, "COOLING": 0, "_lat": []})
    for path in _ledger_files(journal_dir, run_id):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            dur_ms = (rec.get("ts_end", 0.0) - rec.get("ts_start", 0.0)) * 1000.0
            for a in rec.get("attempts", []):
                provider, outcome = a.get("provider"), a.get("outcome")
                if not provider or outcome not in ("DONE", "RATE_LIMITED", "ERROR", "COOLING"):
                    continue
                bucket = agg[provider]
                bucket[outcome] += 1
                if outcome == "DONE" and dur_ms > 0:
                    bucket["_lat"].append(dur_ms)

    out: dict[str, dict[str, Any]] = {}
    for provider, b in sorted(agg.items()):
        decided = b["DONE"] + b["RATE_LIMITED"] + b["ERROR"]
        lat = b["_lat"]
        out[provider] = {
            "done": b["DONE"], "rate_limited": b["RATE_LIMITED"],
            "error": b["ERROR"], "cooling": b["COOLING"],
            "success_rate": round(b["DONE"] / decided, 3) if decided else None,
            "avg_latency_ms": round(sum(lat) / len(lat)) if lat else None,
        }
    return out


def _events(journal_dir: str, run_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    base = Path(journal_dir) / "runs"
    if not base.exists():
        return "", []
    if run_id is None:
        runs = sorted(base.iterdir())
        if not runs:
            return "", []
        run_id = runs[-1].name
    path = base / run_id / "events.jsonl"
    events: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return run_id, events


def run_summary(journal_dir: str = ".iworkflow",
                run_id: str | None = None) -> dict[str, Any]:
    """Summarize one run from its events log (defaults to the most recent run)."""
    run_id, events = _events(journal_dir, run_id)
    if not events:
        return {}
    done = [e for e in events if e.get("event") == "done"]
    resumed = [e for e in events if e.get("event") == "resumed"]
    exhausted = [e for e in events if e.get("event") == "exhausted"]
    routes = [e for e in events if e.get("event") == "route"]
    failovers = [e for e in events if e.get("event") in ("limited", "error")]
    closed = len(done) + len(resumed) + len(exhausted)
    ts = [e["ts"] for e in events if "ts" in e]
    lat = [e["ms"] for e in done if isinstance(e.get("ms"), (int, float))]
    cost = sum(e.get("cost_usd") or 0 for e in done)
    return {
        "run_id": run_id,
        "agents": closed,
        "done": len(done),
        "resumed": len(resumed),
        "exhausted": len(exhausted),
        "failovers": len(failovers),
        "success_rate": round((len(done) + len(resumed)) / closed, 3) if closed else None,
        "by_provider": dict(Counter(e.get("provider") for e in done)),
        "by_kind": dict(Counter(e.get("kind") for e in routes)),
        "duration_ms": round(max(ts) - min(ts), 3) * 1000 if len(ts) > 1 else 0,
        "avg_agent_ms": round(sum(lat) / len(lat)) if lat else None,
        "input_tokens": sum(e.get("input_tokens") or 0 for e in done),
        "output_tokens": sum(e.get("output_tokens") or 0 for e in done),
        "cost_usd": round(cost, 6) if cost else 0,
    }

def print_run_status(recipe_name: str | None = None,
                     spec_path: str | None = None,
                     run_id: str | None = None,
                     journal_dir: str = ".iworkflow") -> None:
    import json
    import time
    
    # 1. Resolve run_id and load events
    run_id, events = _events(journal_dir, run_id)
    if not run_id:
        print("No runs found in journal directory.")
        return

    # 2. Try to load spec
    spec = None
    if spec_path:
        try:
            spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        except Exception:
            pass
    elif recipe_name:
        from .recipes import get_recipe
        try:
            spec = get_recipe(recipe_name)
        except Exception:
            pass

    # Index events by step label
    step_events = defaultdict(list)
    for e in events:
        label = e.get("label")
        if label:
            step_events[label].append(e)

    # Helper to format a step state
    def get_state(label: str) -> tuple[str, str]:
        evs = step_events.get(label) or []
        if not evs:
            # Check if any parent or nested is running
            nested_running = any(
                step_events.get(k)[-1].get("event") == "started"
                for k in step_events
                if k.startswith(label + ":") or k.startswith(label + "#")
            )
            if nested_running:
                return "▶", "\033[33mRUNNING\033[0m"
            return " ", "PENDING"
            
        last = evs[-1]
        evt = last.get("event")
        prov = last.get("provider") or "auto"
        model = last.get("model")
        meta = f" ({prov}:{model})" if model else f" ({prov})"
        
        if evt == "started":
            # Check for heartbeats
            hb = [e for e in evs if e.get("event") == "heartbeat"]
            if hb:
                age = round(time.time() - hb[-1]["ts"])
                return "▶", f"\033[33mRUNNING\033[0m{meta} (HB {age}s ago)"
            return "▶", f"\033[33mRUNNING\033[0m{meta}"
        elif evt in ("done", "resumed"):
            status_text = "\033[32mDONE\033[0m" if evt == "done" else "\033[36mRESUMED\033[0m"
            return "✔", f"{status_text}{meta}"
        elif evt == "checkpoint_pending":
            detail = last.get("validation_error") or last.get("title") or "waiting for human input"
            return "⏸", f"\033[35mPAUSED\033[0m: {detail}"
        elif evt == "error":
            return "✘", f"\033[31mERROR\033[0m{meta}: {last.get('error')}"
        elif evt == "limited":
            return "✘", f"\033[31mLIMITED\033[0m{meta} (Rate Limited)"
        elif evt == "exhausted":
            return "✘", "\033[31mEXHAUSTED\033[0m: All providers failed"
        return " ", "UNKNOWN"

    print(f"\nWorkflow Run Status: {run_id}")
    print("=" * 60)

    if spec:
        steps = spec.get("steps") or []
        for step in steps:
            sid = step.get("id")
            kind = step.get("kind")
            
            # Draw top-level step
            symbol, status = get_state(sid)
            print(f"[{symbol}] {sid:<24} ({kind:<10}) → {status}")
            
            # If parallel, list inner agents
            if kind == "parallel":
                for agent in (step.get("agents") or []):
                    aid = agent.get("id")
                    full_id = f"{sid}:{aid}"
                    symbol_a, status_a = get_state(full_id)
                    print(f"    ├── [{symbol_a}] {aid:<20} → {status_a}")
            
            # If pipeline, check mapped items
            elif kind == "pipeline":
                # Find mapped items from events
                prefix = sid + ":"
                pipeline_steps = sorted({
                    k for k in step_events
                    if k.startswith(prefix)
                })
                for psid in pipeline_steps:
                    symbol_p, status_p = get_state(psid)
                    label_short = psid[len(prefix):]
                    print(f"    ├── [{symbol_p}] {label_short:<20} → {status_p}")
                    
            # If loop, check iterations
            elif kind == "loop":
                prefix = sid + "#"
                loop_steps = sorted({
                    k for k in step_events
                    if k.startswith(prefix)
                })
                for lsid in loop_steps:
                    symbol_l, status_l = get_state(lsid)
                    label_short = lsid[len(prefix):]
                    print(f"    ├── [{symbol_l}] Iter {label_short:<17} → {status_l}")
    else:
        # Chronological dump of known labels from events
        print("No specification provided. Chronological step trace:")
        known_labels = []
        for e in events:
            lbl = e.get("label")
            if lbl and lbl not in known_labels:
                known_labels.append(lbl)
        for lbl in known_labels:
            symbol, status = get_state(lbl)
            print(f"[{symbol}] {lbl:<35} → {status}")

    print("-" * 60)

"""Read run ledger(s) and report per-provider performance.

The data foundation for empirical routing (roadmap #5) and a quick "who's
winning" view: counts of DONE / RATE_LIMITED / ERROR / COOLING per provider,
plus success rate and average DONE latency. Pure reader — no core dependency,
no quota.
"""

from __future__ import annotations

import json
from collections import defaultdict
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

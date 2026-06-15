"""Durable run ledger for iworkflow.

The scheduler can append one record per agent attempt outcome and later recover
completed labels without trusting a possibly partial trailing JSONL line.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def sha(value: Any) -> str:
    """Return a stable 16-character SHA-256 digest for prompts or schemas."""
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class LedgerRecord:
    """One durable record for an agent label in a run."""

    run_id: str
    label: str
    status: str
    provider: str | None
    value: Any
    attempts: list[dict[str, Any]]
    prompt_sha: str
    schema_sha: str | None
    ts_start: float
    ts_end: float
    error_class: str | None
    retry_after: float | None
    # telemetry for analysis / agent crystallization (added 0.2; optional):
    kind: str | None = None                          # routing decision (inferred/role/explicit)
    tools: list[str] = field(default_factory=list)   # tool/skill names injected for this agent
    input_tokens: int | None = None                  # provider-reported usage (best-effort)
    output_tokens: int | None = None
    cost_usd: float | None = None                    # if the provider reports cost (Claude)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping for persistence."""
        return asdict(self)


class RunLedger:
    """Append-only JSONL ledger plus persisted per-provider cooldowns."""

    def __init__(self, run_id: str, dir: str = ".iworkflow") -> None:
        self.run_id = run_id
        self.run_dir = Path(dir) / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "ledger.jsonl"
        self.cooldown_path = self.run_dir / "cooldown.json"

    def append(self, rec: LedgerRecord) -> None:
        """Append one fsynced JSON line for crash-safe resume."""
        line = json.dumps(
            rec.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def load_done(self) -> dict[str, dict[str, Any]]:
        """Load DONE records by label, skipping partial or malformed lines."""
        done: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return done

        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("status") != "DONE":
                    continue
                label = rec.get("label")
                if isinstance(label, str):
                    done[label] = rec
        return done

    def record_cooldown(self, provider: str, until_ts: float) -> None:
        """Persist the timestamp before which a provider must not dispatch."""
        cooldowns = self._load_cooldowns()
        cooldowns[provider] = float(until_ts)
        self._write_json_atomic(self.cooldown_path, cooldowns)

    def is_cooling(self, provider: str, now: float) -> bool:
        """Return True when provider is still inside a persisted cooldown."""
        until_ts = self._load_cooldowns().get(provider)
        return until_ts is not None and until_ts > now

    def _load_cooldowns(self) -> dict[str, float]:
        if not self.cooldown_path.exists():
            return {}
        try:
            raw = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}

        cooldowns: dict[str, float] = {}
        for provider, until_ts in raw.items():
            if isinstance(provider, str) and isinstance(until_ts, int | float):
                cooldowns[provider] = float(until_ts)
        return cooldowns

    def _write_json_atomic(self, path: Path, data: dict[str, float]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RunLedger("selftest", dir=tmp)
        done = LedgerRecord(
            run_id="selftest",
            label="done",
            status="DONE",
            provider="codex",
            value={"ok": True},
            attempts=[{"provider": "codex", "outcome": "DONE", "latency_ms": 7}],
            prompt_sha=sha("prompt"),
            schema_sha=sha({"type": "object"}),
            ts_start=1.0,
            ts_end=2.0,
            error_class=None,
            retry_after=None,
        )
        exhausted = LedgerRecord(
            run_id="selftest",
            label="exhausted",
            status="EXHAUSTED",
            provider=None,
            value=None,
            attempts=[{"provider": "gemini", "outcome": "ERROR", "latency_ms": 3}],
            prompt_sha=sha("prompt-2"),
            schema_sha=None,
            ts_start=3.0,
            ts_end=4.0,
            error_class="ProviderError",
            retry_after=None,
        )

        ledger.append(done)
        ledger.append(exhausted)
        Path(ledger.path).write_text(Path(ledger.path).read_text() + "{bad json\n")

        loaded = ledger.load_done()
        assert loaded == {"done": done.to_dict()}

    print("ledger self-test OK")

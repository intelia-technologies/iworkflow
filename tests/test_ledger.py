from pathlib import Path

from iworkflow.ledger import LedgerRecord, RunLedger, sha


def _record(label, status, provider, value):
    return LedgerRecord(
        run_id="ledger-test",
        label=label,
        status=status,
        provider=provider,
        value=value,
        attempts=[],
        prompt_sha=sha(f"prompt:{label}"),
        schema_sha=None,
        ts_start=1.0,
        ts_end=2.0,
        error_class=None if status == "DONE" else "RATE_LIMITED",
        retry_after=None,
    )


def test_load_done_skips_exhausted_and_malformed_lines(tmp_path):
    ledger = RunLedger("ledger-test", dir=str(tmp_path))
    done = _record("done", "DONE", "codex", {"ok": True})
    exhausted = _record("exhausted", "EXHAUSTED", None, None)

    ledger.append(done)
    ledger.append(exhausted)
    Path(ledger.path).write_text(
        ledger.path.read_text(encoding="utf-8") + "{malformed json\n",
        encoding="utf-8",
    )

    loaded = ledger.load_done()

    assert list(loaded) == ["done"]
    assert loaded["done"] == done.to_dict()


def test_cooldown_persists_across_fresh_ledger(tmp_path):
    ledger = RunLedger("cooldown-test", dir=str(tmp_path))
    ledger.record_cooldown("codex", until_ts=200.0)

    fresh = RunLedger("cooldown-test", dir=str(tmp_path))

    assert fresh.is_cooling("codex", now=199.0) is True
    assert fresh.is_cooling("codex", now=200.0) is False
    assert fresh.is_cooling("codex", now=201.0) is False
    assert fresh.is_cooling("gemini", now=199.0) is False


def test_cooldown_write_leaves_no_tmp_file(tmp_path):
    ledger = RunLedger("cooldown-cleanup", dir=str(tmp_path))

    ledger.record_cooldown("gemini", until_ts=123.0)

    assert ledger.cooldown_path.exists()
    assert not any(path.name.endswith(".tmp") for path in ledger.run_dir.iterdir())

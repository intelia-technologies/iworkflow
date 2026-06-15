#!/usr/bin/env python3
"""Measure Claude input-token cost from injecting an MCP server.

Run manually from this repository. This spends Claude quota.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT = "Reply with exactly: OK"
TIMEOUT_S = 120


def run_and_get_input_tokens(extra_args: list[str]) -> int:
    cmd = [
        "claude",
        "-p",
        PROMPT,
        "--strict-mcp-config",
        "--setting-sources",
        "user",
        "--output-format",
        "json",
        *extra_args,
    ]
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=True,
    )
    data = json.loads(completed.stdout)
    return int(data["usage"]["input_tokens"])


def main() -> None:
    try:
        i0 = run_and_get_input_tokens([])
        print(f"I0 input tokens (no MCP servers): {i0}")

        mcp_config = {
            "mcpServers": {
                "iworkflow": {
                    "command": ".venv/bin/python",
                    "args": ["-m", "iworkflow.mcp_server"],
                }
            }
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".json", delete=False
            ) as f:
                json.dump(mcp_config, f)
                temp_path = f.name

            i1 = run_and_get_input_tokens(["--mcp-config", temp_path])
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)

        print(f"I1 input tokens (1 MCP server): {i1}")
        delta = i1 - i0
        print(f"delta input tokens: {delta}")
        print(
            f"Injecting 1 MCP server cost {delta} input tokens (its tool schemas). "
            "Every server you DON'T inject for a task saves ~that many tokens."
        )
    except Exception as exc:  # noqa: BLE001 - this manual probe must not crash.
        print(_format_error(exc), file=sys.stderr)


def _format_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        parts = [
            f"measure_toolsets.py failed: claude exited with {exc.returncode}",
        ]
        if exc.stdout:
            parts.append(f"stdout:\n{exc.stdout}")
        if exc.stderr:
            parts.append(f"stderr:\n{exc.stderr}")
        return "\n".join(parts)

    if isinstance(exc, subprocess.TimeoutExpired):
        return f"measure_toolsets.py failed: claude timed out after {TIMEOUT_S}s"

    return f"measure_toolsets.py failed: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    main()

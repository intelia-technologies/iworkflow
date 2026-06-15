#!/usr/bin/env python3
"""Orchestrate the PORTABLE catalog loader + the measurement harness WITH iworkflow."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import CodexProvider, GeminiProvider, Runner, log  # noqa: E402

SPEC = r"""Build two things in the iworkflow repo. Keep ALL 43 tests green
(`.venv/bin/python -m pytest -q`). stdlib only (no pyyaml, no new deps).

(A) NEW iworkflow/catalog_loaders.py — a GENERIC, PORTABLE project catalog loader.
It must NOT hardcode any project name: it discovers the catalog from whatever
project DIRECTORY it is pointed at, so it is portable to any repo.

  def load_project_catalog(root: str = ".") -> ToolCatalog
  Reads (each optional — skip if absent, return what is found):
   - <root>/.mcp.json : JSON {"mcpServers": {name: {"command":..,"args":[..]} | {"url":..}}}
       → ToolSpec(name, ToolKind.MCP, description=name, mcp=<that server cfg dict>).
   - <root>/.codex/config.toml : [mcp_servers.<name>] tables (command/args/url) via tomllib
       → ToolSpec(name, ToolKind.MCP, description=name, mcp=<dict>).
   - <root>/.claude/skills/*/SKILL.md  AND  <root>/.claude/skills/*.md :
       parse a leading `--- ... ---` frontmatter block for `name:` and `description:`
       (simple line parse, NOT pyyaml); everything after the frontmatter is the body →
       ToolSpec(name=<frontmatter name or dir/file stem>, ToolKind.SKILL,
                description=<frontmatter description or "">, inject_prompt=<body>).
   - <root>/.claude/commands/*.md :
       → ToolSpec(name=<file stem>, ToolKind.COMMAND, description=<first non-empty line>,
                  inject_prompt=<full file text>).
  De-dupe by name (first registration wins). Be robust to malformed files (skip them).

  tests/test_catalog_loaders.py: build a temp project dir with a .mcp.json (2 servers,
  one command-based one url-based) and one .claude/skills/foo/SKILL.md (frontmatter
  name: foo / description: does foo + a body). Assert load_project_catalog(tmp):
  registers both MCP specs with their mcp cfg, registers the SKILL spec whose
  inject_prompt == the body and description == "does foo". Assert load_project_catalog
  on an empty temp dir returns a catalog whose .all() == [].

(B) NEW examples/measure_toolsets.py — measure the REAL context cost of injected MCP
  tool schemas, using claude -p usage (input tokens):
   - helper run_and_get_input_tokens(extra_args: list[str]) that runs:
       claude -p "Reply with exactly: OK" --strict-mcp-config --setting-sources user
              --output-format json  <extra_args...>
     via subprocess (timeout ~120s), parses stdout JSON, returns int(usage.input_tokens).
   - I0 = run with NO extra args (no MCP servers).
   - write a temp JSON mcp-config:
       {"mcpServers":{"iworkflow":{"command":".venv/bin/python","args":["-m","iworkflow.mcp_server"]}}}
     I1 = run with ["--mcp-config", <that temp file>].
   - print I0, I1, and delta = I1 - I0, with a line:
     "Injecting 1 MCP server cost {delta} input tokens (its tool schemas). Every
      server you DON'T inject for a task saves ~that many tokens."
   - tolerate failures (print the error, don't crash). This file is run manually by a
     human; do NOT run it yourself.

Run `.venv/bin/python -m pytest -q` yourself (43 existing + your loader tests) and
confirm green before reporting. Do NOT run measure_toolsets.py (it spends quota)."""

REPORT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "files_touched", "summary"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "files_touched": {"type": "array", "items": {"type": "string"}},
                   "summary": {"type": "string"}},
}


async def main():
    runner = Runner(
        run_id="build-catalog-measure-1",
        providers={"codex": CodexProvider("codex", timeout_s=420),
                   "gemini": GeminiProvider("gemini", timeout_s=220)},
        caps={"codex": 1, "gemini": 1})

    log("PHASE implement (codex, workspace-write)")
    impl = await runner.agent(
        SPEC, label="implement:catalog-measure", schema=REPORT,
        prefer=["codex"], sandbox="workspace-write")
    print(f"\nIMPLEMENT [{impl.provider}] → {impl.value}")

    log("PHASE audit (gemini, read-only)")
    audit = await runner.agent(
        "Read iworkflow/catalog_loaders.py and examples/measure_toolsets.py. Is the "
        "loader PORTABLE (no hardcoded project name, reads from the given root) and "
        "robust to missing/malformed files? Does the measurement correctly parse claude "
        "-p usage.input_tokens and compute the delta? Any bug? 6 sentences max.",
        label="audit:catalog-measure", prefer=["gemini", "codex"])
    print(f"\nAUDIT [{audit.provider}] →\n{audit.value}")
    return {"implement": impl.value, "audit": audit.value}


if __name__ == "__main__":
    asyncio.run(main())

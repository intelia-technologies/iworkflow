#!/usr/bin/env python3
"""Orchestrate the dynamic-toolset MVP (Phase 1) WITH iworkflow.

  implement → Codex (workspace-write, single) builds toolsets.py + threads a
              `tools=`/`toolset=` param through scheduler + providers + injection
  audit     → Gemini (read-only) reviews backward-compat + injection correctness

Every new param defaults to None/empty so the existing 36 tests stay green.
Parent verifies independently.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from iworkflow import CodexProvider, GeminiProvider, Runner, log  # noqa: E402

SPEC = r"""Implement Phase 1 MVP of dynamic per-task toolset injection. READ
docs/design/dynamic-toolsets.md first. Keep ALL existing tests green
(`.venv/bin/python -m pytest -q` is 36 passing). EVERY new parameter must DEFAULT
to None/empty so current callers and tests are unaffected.

(A) NEW file iworkflow/toolsets.py (stdlib only):
  - ToolKind(enum.StrEnum): MCP, SKILL, COMMAND, NATIVE (UPPERCASE name==value).
  - @dataclass(frozen=True) ToolSpec: name:str, kind:ToolKind, description:str,
    tags:tuple[str,...]=(), mcp:dict|None=None, allow_tools:tuple[str,...]=(),
    inject_prompt:str|None=None.
  - @dataclass(frozen=True) ToolSet: specs:tuple[ToolSpec,...]=().
      .mcp_servers()->dict[str,dict]  (name->mcp cfg for kind==MCP specs)
      .allowed()->list[str]  (every spec's allow_tools + the name of NATIVE specs,
                              order-stable, de-duplicated)
      .prompt_additions()->str  ("\n\n".join of inject_prompt for SKILL/COMMAND, in order)
      .is_empty()->bool
  - class ToolCatalog: register(spec); resolve(keys:list[str])->ToolSet (a spec
    matches if spec.name==key OR key in spec.tags; preserve catalog insertion order;
    de-dupe; unknown keys ignored); all()->list[ToolSpec]; search(query:str,k:int)->
    ToolSet (score each spec by how many whitespace-split query words appear in its
    name+description+tags, return top-k by score>0).
  - `if __name__=='__main__':` self-test: register an MCP spec, a SKILL spec, a NATIVE
    spec; resolve by name and by tag; assert mcp_servers(), allowed(), and
    prompt_additions() are correct; print 'toolsets self-test OK'.

(B) Threading — defaults are no-ops:
  - iworkflow/scheduler.py Runner.__init__: add `catalog: ToolCatalog | None = None`
    -> self.catalog. agent(): add `tools: list[str] | None = None`; compute
    `toolset = self.catalog.resolve(tools) if (tools and self.catalog) else None` and
    pass `toolset=toolset` into prov.run(...).
  - iworkflow/providers.py: add `toolset=None` to Provider.run and to EVERY provider's
    run (CodexProvider, ClaudeProvider, ClaudeInteractiveProvider, GeminiProvider,
    FakeProvider). FakeProvider just ignores it.

(C) Injection (only when toolset is not None and not is_empty()):
  - CodexProvider: prepend toolset.prompt_additions() (+ "\n\n") to the prompt when
    non-empty. For each name,cfg in toolset.mcp_servers(): add `--ignore-user-config`
    ONCE and `-c mcp_servers.<name>.command="<cmd>"` plus `-c mcp_servers.<name>.args=
    [<json-ish toml array>]` (or `-c mcp_servers.<name>.url="<url>"` when cfg has 'url').
    Codex has no clean native-tool allow-list flag — add a brief comment, do NOT invent one.
  - ClaudeProvider: prepend prompt_additions to the piped prompt. If mcp_servers():
    write a temp JSON file {"mcpServers": {name: cfg, ...}} and add `--mcp-config <file>`
    (it already passes --strict-mcp-config). If allowed(): add `--allowedTools` followed
    by the names. Clean up the temp file.
  - ClaudeInteractiveProvider + GeminiProvider: at minimum prepend prompt_additions to
    the prompt when present. Accept the toolset param regardless.

(D) Tests — tests/test_toolsets.py (+ extend if needed):
  - ToolSet.mcp_servers/allowed/prompt_additions from a mix of MCP+SKILL+NATIVE specs.
  - ToolCatalog.resolve by name and by tag; unknown key ignored; search() top-k.
  - A Runner with a catalog and tools=[...] forwards a non-None ToolSet with the
    resolved specs to the provider (use a small recording Provider subclass that stores
    the toolset it received). With tools=None OR no catalog, the provider receives
    toolset=None.

Run `python3 iworkflow/toolsets.py` and `.venv/bin/python -m pytest -q` yourself and
confirm BOTH pass (36 existing + your new tests) before reporting."""

REPORT = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "files_touched", "summary"],
    "properties": {"verdict": {"type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"]},
                   "files_touched": {"type": "array", "items": {"type": "string"}},
                   "summary": {"type": "string"}},
}


async def main():
    runner = Runner(
        run_id="build-toolsets-1",
        providers={"codex": CodexProvider("codex", timeout_s=540),
                   "gemini": GeminiProvider("gemini", timeout_s=240)},
        caps={"codex": 1, "gemini": 1})

    log("PHASE implement (codex, workspace-write)")
    impl = await runner.agent(
        SPEC, label="implement:toolsets", schema=REPORT,
        prefer=["codex"], sandbox="workspace-write")
    print(f"\nIMPLEMENT [{impl.provider}] → {impl.value}")

    log("PHASE audit (gemini, read-only)")
    audit = await runner.agent(
        "Read iworkflow/toolsets.py and the toolset threading in iworkflow/scheduler.py "
        "+ providers.py. Is it backward-compatible (all new params default to no-op)? "
        "Is the per-provider injection correct (Codex -c mcp_servers, Claude --mcp-config "
        "+ --allowedTools, prompt_additions prepended)? Any bug that would break the 36 "
        "existing tests or mis-inject? 6 sentences max.",
        label="audit:toolsets", prefer=["gemini", "codex"])
    print(f"\nAUDIT [{audit.provider}] →\n{audit.value}")
    return {"implement": impl.value, "audit": audit.value}


if __name__ == "__main__":
    asyncio.run(main())

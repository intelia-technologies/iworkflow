# Design: dynamic per-task toolset injection

Status: proposal · Author: drafted with Pablo · Scope: iworkflow core (generic)

## Problem

Every agent CLI loads its **whole** catalog at startup — all MCP servers, all
skills, all commands (in Codex via profiles; in Claude via project settings).
For a focused worker task ("review this file for bugs") that means injecting
dozens of tool schemas and skill descriptions the task will never use.

This is wasteful on three axes that matter especially for a **subscription-only**
orchestrator:

- **Quota.** In subscription-only mode, every context token a worker reads is
  drawn from your limited pool (Claude's weekly Pool-1, Codex/Gemini usage).
  Loading 70 tools for a 5-tool task burns the limit on noise.
- **Focus / quality.** A model offered 70 tools makes worse tool choices than one
  offered the 5 relevant ones (more distraction, more wrong calls).
- **Catalog scale.** Intelia's real catalog is ~275 capabilities + many MCP
  servers + ~50 skills + commands. At that scale you *cannot* inject everything —
  selection stops being an optimization and becomes a requirement.

Claude Code already solves this for its own loop via **deferred tools** loaded on
demand (`ToolSearch`). This design brings the same idea to the **workers iworkflow
orchestrates** (Codex / Gemini / Claude), per task.

## Goals / non-goals

**Goals**
- Inject only the tools/skills/commands a task needs, per `agent()` call.
- Stay **provider-agnostic** in the core; each adapter translates a selected
  toolset into its CLI's flags.
- Keep iworkflow **catalog-agnostic**: the catalog is a generic registry; Intelia
  (or any user) supplies its catalog through a loader. No Intelia coupling in core.
- Be measurable: prove the context/quota savings empirically.

**Non-goals (for v1)**
- Perfect automatic selection. Start explicit; evolve toward inference.
- Changing how the CLIs themselves work — we only use their existing flags.

## Architecture — three layers

```
 workflow author / selector
        │  tools=[...]  (names or tags)
        ▼
 ┌───────────────┐   resolve    ┌──────────────┐
 │ ToolCatalog   │ ───────────► │  ToolSet      │  (the chosen subset)
 │ (generic reg) │              └──────┬───────┘
 └───────────────┘                     │ per-provider injection
   ▲ loaded by a CatalogProvider       ▼
   │ (Intelia loader reads .mcp.json,  Provider adapter → CLI flags
   │  .claude/skills, .codex/config)   (claude --mcp-config / codex -c / agy)
```

1. **Catalog registry** — a generic in-memory registry of `ToolSpec`s.
2. **Selection** — pick a `ToolSet` for a task (explicit → tags → retrieval).
3. **Injection** — each provider adapter renders the `ToolSet` into its flags.

## Data model (generic, in iworkflow)

```python
class ToolKind(StrEnum):
    MCP = "MCP"          # an MCP server (command/args or url)
    SKILL = "SKILL"      # an instruction block (prompt-injected on demand)
    COMMAND = "COMMAND"  # a slash-command / recipe (prompt-injected)
    NATIVE = "NATIVE"    # a built-in CLI tool to allow (Bash, Read, …)

@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    description: str            # used for selection (keyword / future embedding)
    tags: tuple[str, ...] = ()
    # injection intent (provider-agnostic):
    mcp: McpServerConfig | None = None      # for MCP: {command,args} or {url}
    allow_tools: tuple[str, ...] = ()       # native tool names to allow
    inject_prompt: str | None = None        # for SKILL/COMMAND: text to append

@dataclass(frozen=True)
class ToolSet:
    specs: tuple[ToolSpec, ...]
    # convenience views the adapters use:
    def mcp_servers(self) -> dict[str, McpServerConfig]: ...
    def allowed(self) -> list[str]: ...
    def prompt_additions(self) -> str: ...   # concatenated SKILL/COMMAND blocks

class ToolCatalog:
    def register(self, spec: ToolSpec) -> None: ...
    def resolve(self, names_or_tags: list[str]) -> ToolSet: ...
    def search(self, query: str, k: int) -> ToolSet: ...   # phase 3
    def all(self) -> list[ToolSpec]: ...
```

**Catalog source (decided: generic/OSS).** iworkflow ships an empty `ToolCatalog`
+ a `CatalogProvider` protocol. Intelia supplies its catalog via its own loader
(a separate module that reads `.mcp.json`, `.claude/skills/`, `.codex/config.toml`
and calls `catalog.register(...)`). iworkflow core never imports Intelia.

```python
class CatalogProvider(Protocol):
    def populate(self, catalog: ToolCatalog) -> None: ...
# Intelia ships e.g. IntelaCatalogProvider; iworkflow ships none (or tiny demo).
```

## Selection strategies (phased)

- **Phase 1 — explicit.** `agent(prompt, tools=["postgres", "holded.read"])`.
  Names/tags resolved against the catalog. Deterministic; the workflow author
  knows what each phase needs. **This is the MVP.**
- **Phase 2 — tags / keywords.** A `select(task, catalog)` that matches task text
  or a declared `needs=("database","banking")` against `ToolSpec.tags`/description.
- **Phase 3 — tool-RAG.** Embedding retrieval over tool descriptions for the
  ~275-item catalog (`catalog.search(task, k=8)`). Needs an embedding source
  (cost/where = open question). Optionally a cheap LLM pre-selector instead.

## Per-provider injection (the lossy part)

| Intent | Claude | Codex | agy/Gemini |
|---|---|---|---|
| Only these MCP servers | `--mcp-config <json>` + `--strict-mcp-config` | `--ignore-user-config` + `-c mcp_servers.<n>...` | write a temp `mcp_config.json` |
| Allow only these native tools | `--allowedTools <names>` | execpolicy / approval (coarse) | — (coarse) |
| Skill/command instructions | append to prompt or `--append-system-prompt` | prepend to prompt | prepend to prompt |

Notes:
- **Claude is the most granular** (mcp-config + allowedTools + setting-sources) —
  we already start workers lean with `--strict-mcp-config --setting-sources user`,
  so this extends an existing hook.
- **Codex** does per-call MCP injection via `-c mcp_servers.*` (proven in the MCP
  spike) on top of `--ignore-user-config` for a clean base.
- **agy** is the least granular — likely a per-invocation `mcp_config.json` and
  prompt injection; native-tool allow-listing may not be controllable.
- **SKILL/COMMAND injection is pure prompt context.** A skill is just instructions;
  "injecting a skill" = appending its body to the worker prompt *only when selected*
  (vs always-loaded). This is the biggest context win for the Intelia catalog
  (skill bodies are KBs each).

## API surface

```python
await runner.agent(
    "Debug why this reconciliation didn't match",
    label="debug", role="reason",
    tools=["postgres", "debug-reconciliation", "Bash", "Read"],  # names/tags
)
# or a pre-built ToolSet (from a selector):
ts = catalog.resolve(["banking"])           # tag → set
await runner.agent(prompt, label="x", toolset=ts)
```

The Runner resolves `tools` → `ToolSet` via the catalog, then passes it to
`prov.run(..., toolset=ts)`. Adapters render it. Default `tools=None` = today's
behaviour (whatever the CLI loads by default), so this is fully backward-compatible.

## The lean-vs-safe tension (key risk)

Inject too little and the worker fails mid-task lacking a tool. Mitigations:

- **Core set always injected** — a small always-on set per task *kind* (e.g. code
  tasks always get `Bash/Read/Edit`), configurable.
- **Additive selection** — selected tools layer on top of the core.
- **Missing-tool feedback loop (later)** — if a worker errors citing an
  unavailable tool, the scheduler re-dispatches once with a broader set. Cheap
  insurance against under-selection.
- **Start conservative**, measure, then tighten.

## Measurement plan

The CLIs report token usage (`claude -p --output-format json` envelope `.usage`;
Codex similarly). A benchmark runs representative tasks twice — full catalog vs
lean injection — and reports input-token delta, latency, and success rate. The
ledger already records latency; extend a record field for input tokens so
`stats.py` can show "tokens saved" per provider. **No claim of savings ships
without this measurement.**

## Phasing

1. **MVP** — `ToolSpec`/`ToolSet`/`ToolCatalog` + explicit `tools=` + Claude &
   Codex injection (best flags) + a measurement harness. (agy: prompt-injection
   + temp mcp_config only.)
2. Tag/keyword `select()` + a small always-on core set.
3. Intelia `CatalogProvider` (reads its real catalog) — lives in intelia-admin,
   not iworkflow.
4. Tool-RAG retrieval for the full 275-item catalog; optional missing-tool loop.

## Open questions

- Selection accuracy / how to evaluate over- vs under-selection.
- agy granularity — is native-tool allow-listing possible at all?
- Embedding source + cost for tool-RAG (and whether a cheap LLM selector beats it).
- Catalog freshness — Intelia's catalog changes; the loader must re-read per run.
- Does injecting an MCP server per call add startup latency that eats the savings?
  (measure: MCP server spawn time vs context tokens saved.)
```

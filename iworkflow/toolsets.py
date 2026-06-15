"""Generic tool catalog and per-task toolset views.

This module is intentionally provider-agnostic and stdlib-only. Workflow authors
or external catalog loaders register generic ToolSpec entries; providers decide
how to translate a selected ToolSet into CLI flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ToolKind(StrEnum):
    MCP = "MCP"
    SKILL = "SKILL"
    COMMAND = "COMMAND"
    NATIVE = "NATIVE"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    description: str
    tags: tuple[str, ...] = ()
    mcp: dict[str, Any] | None = None
    allow_tools: tuple[str, ...] = ()
    inject_prompt: str | None = None


@dataclass(frozen=True)
class ToolSet:
    specs: tuple[ToolSpec, ...] = ()

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return {
            spec.name: spec.mcp
            for spec in self.specs
            if spec.kind == ToolKind.MCP and spec.mcp is not None
        }

    def allowed(self) -> list[str]:
        allowed: list[str] = []
        seen: set[str] = set()
        for spec in self.specs:
            for name in spec.allow_tools:
                if name not in seen:
                    allowed.append(name)
                    seen.add(name)
            if spec.kind == ToolKind.NATIVE and spec.name not in seen:
                allowed.append(spec.name)
                seen.add(spec.name)
        return allowed

    def prompt_additions(self) -> str:
        return "\n\n".join(
            spec.inject_prompt
            for spec in self.specs
            if spec.kind in (ToolKind.SKILL, ToolKind.COMMAND) and spec.inject_prompt
        )

    def is_empty(self) -> bool:
        return not self.specs


class ToolCatalog:
    def __init__(self) -> None:
        self._specs: list[ToolSpec] = []

    def register(self, spec: ToolSpec) -> None:
        self._specs.append(spec)

    def resolve(self, keys: list[str]) -> ToolSet:
        if not keys:
            return ToolSet()
        wanted = set(keys)
        specs: list[ToolSpec] = []
        seen: set[str] = set()
        for spec in self._specs:
            if spec.name in seen:
                continue
            if spec.name in wanted or any(tag in wanted for tag in spec.tags):
                specs.append(spec)
                seen.add(spec.name)
        return ToolSet(tuple(specs))

    def all(self) -> list[ToolSpec]:
        return list(self._specs)

    def search(self, query: str, k: int) -> ToolSet:
        if k <= 0:
            return ToolSet()
        words = query.lower().split()
        if not words:
            return ToolSet()

        scored: list[tuple[int, int, ToolSpec]] = []
        for index, spec in enumerate(self._specs):
            haystack = " ".join(
                (spec.name, spec.description, " ".join(spec.tags))
            ).lower()
            score = sum(1 for word in words if word in haystack)
            if score > 0:
                scored.append((score, index, spec))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return ToolSet(tuple(spec for _, _, spec in scored[:k]))


if __name__ == "__main__":
    catalog = ToolCatalog()
    mcp_cfg = {"command": "postgres-mcp", "args": ["--readonly"]}
    mcp = ToolSpec(
        "postgres",
        ToolKind.MCP,
        "Query Postgres",
        tags=("database",),
        mcp=mcp_cfg,
        allow_tools=("mcp__postgres__query",),
    )
    skill = ToolSpec(
        "review-skill",
        ToolKind.SKILL,
        "Review code",
        tags=("review",),
        inject_prompt="Use the review checklist.",
    )
    native = ToolSpec("Bash", ToolKind.NATIVE, "Run shell commands", tags=("shell",))
    catalog.register(mcp)
    catalog.register(skill)
    catalog.register(native)

    resolved = catalog.resolve(["postgres", "review", "shell"])
    assert resolved.specs == (mcp, skill, native)
    assert resolved.mcp_servers() == {"postgres": mcp_cfg}
    assert resolved.allowed() == ["mcp__postgres__query", "Bash"]
    assert resolved.prompt_additions() == "Use the review checklist."
    assert catalog.resolve(["database"]).mcp_servers() == {"postgres": mcp_cfg}
    print("toolsets self-test OK")

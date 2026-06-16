"""Portable project catalog loaders.

The loader in this module discovers tool metadata from the project directory it
is pointed at. It intentionally stays generic and stdlib-only.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .toolsets import ToolCatalog, ToolKind, ToolSpec


def load_project_catalog(root: str = ".") -> ToolCatalog:
    """Load a generic tool catalog from optional project-local config files."""

    project = Path(root)
    catalog = ToolCatalog()
    seen: set[str] = set()

    def register_once(spec: ToolSpec) -> None:
        if not spec.name or spec.name in seen:
            return
        catalog.register(spec)
        seen.add(spec.name)

    for name, cfg in _load_mcp_json(project / ".mcp.json"):
        register_once(ToolSpec(name, ToolKind.MCP, name, mcp=cfg))

    for name, cfg in _load_codex_mcp(project / ".codex" / "config.toml"):
        register_once(ToolSpec(name, ToolKind.MCP, name, mcp=cfg))

    skills_dir = project / ".claude" / "skills"
    for path in _skill_paths(skills_dir):
        spec = _load_skill(path)
        if spec is not None:
            register_once(spec)

    codex_skills_dir = project / ".codex" / "skills"
    for path in _skill_paths(codex_skills_dir):
        spec = _load_skill(path)
        if spec is not None:
            register_once(spec)

    commands_dir = project / ".claude" / "commands"
    for path in _command_paths(commands_dir):
        name = _command_name(commands_dir, path)
        spec = _load_command(path, name)
        if spec is not None:
            register_once(spec)

    return catalog


def _load_mcp_json(path: Path) -> list[tuple[str, dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []

    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []

    loaded: list[tuple[str, dict[str, Any]]] = []
    for name, cfg in servers.items():
        if isinstance(name, str) and isinstance(cfg, dict):
            loaded.append((name, cfg))
    return loaded


def _load_codex_mcp(path: Path) -> list[tuple[str, dict[str, Any]]]:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []

    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []

    loaded: list[tuple[str, dict[str, Any]]] = []
    for name, cfg in servers.items():
        if isinstance(name, str) and isinstance(cfg, dict):
            loaded.append((name, dict(cfg)))
    return loaded


def _skill_paths(skills_dir: Path) -> list[Path]:
    if not skills_dir.is_dir():
        return []
    return sorted(skills_dir.glob("*/SKILL.md")) + sorted(skills_dir.glob("*.md"))


def _command_paths(commands_dir: Path) -> list[Path]:
    if not commands_dir.is_dir():
        return []
    return sorted(
        path for path in commands_dir.rglob("*.md") if path.name.lower() != "readme.md"
    )


def _command_name(commands_dir: Path, path: Path) -> str:
    relative = path.relative_to(commands_dir).with_suffix("")
    return ":".join(relative.parts)


def _load_skill(path: Path) -> ToolSpec | None:
    text = _read_text(path)
    if text is None:
        return None

    parsed = _split_frontmatter(text)
    if parsed is None:
        return None

    metadata, body = parsed
    fallback_name = path.parent.name if path.name == "SKILL.md" else path.stem
    name = metadata.get("name") or fallback_name
    description = metadata.get("description", "")
    return ToolSpec(name, ToolKind.SKILL, description, inject_prompt=body)


def _load_command(path: Path, name: str) -> ToolSpec | None:
    text = _read_text(path)
    if text is None:
        return None

    parsed = _split_frontmatter(text)
    if parsed is not None:
        metadata, body = parsed
        description = metadata.get("description") or _first_nonempty_line(body)
    else:
        description = _first_nonempty_line(text)

    return ToolSpec(
        name,
        ToolKind.COMMAND,
        description,
        inject_prompt=text,
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _split_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        return None

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        if key in ("name", "description"):
            metadata[key] = _strip_scalar(value.strip())

    return metadata, "".join(lines[end + 1 :])


def _strip_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""

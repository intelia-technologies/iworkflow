"""Catalog of subscription CLI providers and the models each exposes.

One provider id per vendor (codex, claude, gemini, cursor). Workflows pick a
model per agent via `model` (single target) or `models` (per-provider map), or
rely on routing hints in `routing.KIND_MODEL_HINTS`.

Legacy alias `cursor_flash` -> provider `cursor` + model `composer-2.5-flash`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROVIDER_MODELS: dict[str, dict[str, Any]] = {
    "codex": {
        "label": "OpenAI Codex (ChatGPT subscription)",
        "cli": "codex exec -m <model>",
        "login": "codex login",
        "default": None,
        "models": {
            "gpt-5.4": {
                "label": "GPT-5.4",
                "aliases": ["5.4"],
                "notes": "Structured codegen; native --output-schema",
            },
            "gpt-5.5": {
                "label": "GPT-5.5",
                "aliases": ["5.5", "default"],
            },
        },
    },
    "claude": {
        "label": "Claude (interactive TUI via tmux, Pool 1 subscription)",
        "cli": "claude --model <model>",
        "login": "claude auth login",
        "default": None,
        "models": {
            "opus": {
                "label": "Claude Opus",
                "aliases": ["claude-opus-4-6"],
                "notes": "Deep reasoning; scarce weekly quota",
            },
            "sonnet": {
                "label": "Claude Sonnet",
                "aliases": ["claude-sonnet-4-6", "sonnet-4.6"],
            },
        },
    },
    "gemini": {
        "label": "Gemini (agy / Antigravity, Google subscription)",
        "cli": "agy -p --model <name>",
        "login": "Antigravity / agy install (agy models lists session)",
        "default": "Gemini 3.5 Flash (Medium)",
        "models": {
            "Gemini 3.5 Flash (Medium)": {
                "label": "Gemini 3.5 Flash (Medium)",
                "aliases": ["gemini-3.5-flash", "flash", "3.5-flash"],
            },
            "Gemini 3.5 Flash (High)": {
                "label": "Gemini 3.5 Flash (High)",
                "aliases": ["flash-high"],
            },
            "Gemini 3.5 Flash (Low)": {
                "label": "Gemini 3.5 Flash (Low)",
                "aliases": ["flash-low"],
            },
            "Gemini 3.1 Pro (High)": {
                "label": "Gemini 3.1 Pro (High)",
                "aliases": ["pro", "gemini-3.1-pro"],
            },
        },
    },
    "cursor": {
        "label": "Cursor Agent (Cursor subscription)",
        "cli": "cursor-agent -p --model <model>",
        "login": "cursor-agent login",
        "default": "composer-2.5",
        "models": {
            "composer-2.5": {
                "label": "Composer 2.5",
                "aliases": ["composer", "2.5"],
                "notes": "Repo-aware codegen and review",
            },
            "composer-2.5-fast": {
                "label": "Composer 2.5 Fast",
                "aliases": ["flash", "composer-flash", "2.5-fast", "composer-2.5-flash"],
                "notes": "Fast fan-out / classify (cursor-agent default)",
            },
        },
    },
}

LEGACY_PROVIDER_ALIASES: dict[str, tuple[str, str | None]] = {
    "cursor_flash": ("cursor", "composer-2.5-fast"),
}

RoutingTarget = tuple[str, str | None]

# Load dynamic model config override
MODELS_FILE = Path.home() / ".iworkflow" / "models.json"
if MODELS_FILE.is_file():
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            providers = data.get("providers") if "providers" in data else data
            if isinstance(providers, dict):
                PROVIDER_MODELS.update(providers)
    except Exception:
        pass


def _alias_index() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for provider, meta in PROVIDER_MODELS.items():
        for model_id, info in (meta.get("models") or {}).items():
            out[(provider, model_id.lower())] = model_id
            for alias in info.get("aliases") or []:
                out[(provider, str(alias).lower())] = model_id
    return out


_ALIAS_INDEX: dict[tuple[str, str], str] | None = None


def _aliases() -> dict[tuple[str, str], str]:
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        _ALIAS_INDEX = _alias_index()
    return _ALIAS_INDEX


def list_provider_models() -> dict[str, Any]:
    return {
        "providers": {
            name: {
                "label": meta["label"],
                "cli": meta["cli"],
                "login": meta.get("login"),
                "default": meta.get("default"),
                "models": [
                    {
                        "id": mid,
                        "label": info.get("label", mid),
                        "aliases": list(info.get("aliases") or []),
                        "notes": info.get("notes"),
                    }
                    for mid, info in (meta.get("models") or {}).items()
                ],
            }
            for name, meta in PROVIDER_MODELS.items()
        },
        "legacy_aliases": {
            old: {"provider": prov, "model": model}
            for old, (prov, model) in LEGACY_PROVIDER_ALIASES.items()
        },
    }


def default_model(provider: str) -> str | None:
    meta = PROVIDER_MODELS.get(provider) or {}
    return meta.get("default")


def resolve_model(provider: str, model: str | None) -> str | None:
    if model is None:
        return None
    return _aliases().get((provider, model.lower()), model)


def parse_prefer_entry(entry: str | dict[str, Any]) -> RoutingTarget:
    if isinstance(entry, dict):
        prov = str(entry.get("provider") or entry.get("name") or "")
        if not prov:
            raise ValueError("prefer object needs 'provider'")
        raw_model = entry.get("model")
        if prov in LEGACY_PROVIDER_ALIASES:
            base, leg_model = LEGACY_PROVIDER_ALIASES[prov]
            return base, resolve_model(base, raw_model or leg_model)
        return prov, resolve_model(prov, raw_model)

    text = str(entry).strip()
    if not text:
        raise ValueError("empty prefer entry")
    if text in LEGACY_PROVIDER_ALIASES:
        prov, model = LEGACY_PROVIDER_ALIASES[text]
        return prov, model
    if ":" in text:
        prov, _, raw = text.partition(":")
        prov = prov.strip()
        raw = raw.strip() or None
        if prov in LEGACY_PROVIDER_ALIASES:
            base, leg_model = LEGACY_PROVIDER_ALIASES[prov]
            return base, resolve_model(base, raw or leg_model)
        return prov, resolve_model(prov, raw)
    return text, None


def parse_prefer_list(
    prefer: list[str | dict[str, Any]] | None,
    *,
    model: str | None = None,
    models: dict[str, str] | None = None,
) -> list[RoutingTarget]:
    if not prefer:
        return []
    per_provider = {k: resolve_model(k, v) for k, v in (models or {}).items()}
    out: list[RoutingTarget] = []
    for entry in prefer:
        prov, entry_model = parse_prefer_entry(entry)
        resolved = entry_model or per_provider.get(prov)
        if resolved is None and model is not None and len(prefer) == 1:
            resolved = resolve_model(prov, model)
        out.append((prov, resolved))
    return out


def format_prefer(targets: list[RoutingTarget]) -> str:
    parts: list[str] = []
    for prov, m in targets:
        parts.append(f"{prov}:{m}" if m else prov)
    return ", ".join(parts)

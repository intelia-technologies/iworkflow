"""Catalog of subscription CLI providers and the models each exposes.

One provider id per vendor (codex, claude, gemini, cursor). Workflows pick a
model per agent via `model` (single target) or `models` (per-provider map), or
rely on routing hints in `routing.KIND_MODEL_HINTS`.

Legacy alias `cursor_flash` -> provider `cursor` + model `composer-2.5` (the
-fast tier is the same model with extra cost, so nothing routes to it).
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
        "scarcity": "low",
        "cap": 2,
        "timeout_s": 300,
        "default": None,
        "models": {
            # GPT-5.6 family (verified against codex CLI + ChatGPT account 2026-07-10):
            # luna = fast/cheap, terra = balanced default, sol = top capability.
            "gpt-5.6-luna": {
                "label": "GPT-5.6 Luna",
                "aliases": ["luna", "5.6-luna"],
                "notes": "Fast/efficient profile — bulk fan-out, classify, small edits",
            },
            "gpt-5.6-terra": {
                "label": "GPT-5.6 Terra",
                "aliases": ["terra", "5.6-terra"],
                "notes": "Balanced profile — implementation, review, audit (routing default)",
            },
            "gpt-5.6-sol": {
                "label": "GPT-5.6 Sol",
                "aliases": ["sol", "5.6-sol"],
                "notes": "Top of the 5.6 family — architecture, hard debugging, long tasks",
            },
            "gpt-5.5": {
                "label": "GPT-5.5",
                "aliases": ["5.5", "default"],
                "notes": "Known-stable fallback; keep for tasks tuned on it",
            },
        },
    },
    "claude": {
        "label": "Claude (claude -p headless, subscription)",
        "cli": "claude -p --model <model>",
        "login": "claude auth login",
        "scarcity": "high",
        "cap": 1,
        "timeout_s": 600,
        "default": None,
        "models": {
            "opus": {
                "label": "Claude Opus",
                "aliases": ["claude-opus-4-8"],
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
        "scarcity": "low",
        "cap": 2,
        "timeout_s": 420,
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
        "scarcity": "medium",
        "cap": 2,
        "timeout_s": 150,
        "default": "composer-2.5",
        "models": {
            "composer-2.5": {
                "label": "Composer 2.5",
                "aliases": ["composer", "2.5", "flash", "composer-flash",
                            "2.5-fast", "composer-2.5-fast", "composer-2.5-flash"],
                "notes": "Repo-aware codegen and review. The -fast tier is the "
                         "same model with extra-cost priority — deliberately "
                         "unlisted; all fast/flash aliases resolve here.",
            },
        },
    },
}

LEGACY_PROVIDER_ALIASES: dict[str, tuple[str, str | None]] = {
    "cursor_flash": ("cursor", "composer-2.5"),  # -fast tier costs extra; never route to it
}

RoutingTarget = tuple[str, str | None]

def _merge_provider_models(providers: dict[str, Any]) -> None:
    """Deep-merge a user/model override into PROVIDER_MODELS, per provider: keys the
    user specifies win, but unspecified keys (e.g. the built-in scarcity/cap/timeout
    profile) survive instead of being wiped by a wholesale replace."""
    for name, meta in providers.items():
        existing = PROVIDER_MODELS.get(name)
        if isinstance(meta, dict) and isinstance(existing, dict):
            PROVIDER_MODELS[name] = {**existing, **meta}
        else:
            PROVIDER_MODELS[name] = meta


# Load dynamic model config override
MODELS_FILE = Path.home() / ".iworkflow" / "models.json"
if MODELS_FILE.is_file():
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            providers = data.get("providers") if "providers" in data else data
            if isinstance(providers, dict):
                _merge_provider_models(providers)
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


def default_timeout(provider: str) -> int | None:
    """Per-provider CLI timeout ceiling (seconds). None → caller picks a fallback."""
    meta = PROVIDER_MODELS.get(provider) or {}
    value = meta.get("timeout_s")
    return int(value) if value is not None else None


def default_cap(provider: str) -> int:
    """Per-provider concurrency cap. Explicit `cap` wins; else derive from scarcity
    (high -> 1, the scarce shared pool; otherwise -> 2)."""
    meta = PROVIDER_MODELS.get(provider) or {}
    cap = meta.get("cap")
    if cap is not None:
        return int(cap)
    return 1 if meta.get("scarcity") == "high" else 2


def provider_scarcity(provider: str) -> str:
    """Subscription scarcity tier: 'low' | 'medium' | 'high' (default 'low').
    Used by the scheduler's idle-spill so a scarce provider is never promoted."""
    meta = PROVIDER_MODELS.get(provider) or {}
    return str(meta.get("scarcity") or "low")


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

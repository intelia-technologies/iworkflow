"""Dependency-free minimal JSON-Schema check for the spike.

Production would use `jsonschema`. For the spike we only need: required keys,
enum membership, and additionalProperties:false. Returns (ok, error)."""

from __future__ import annotations

from typing import Any


def validate(obj: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    if schema.get("type") == "object" and not isinstance(obj, dict):
        return False, f"expected object, got {type(obj).__name__}"
    props: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in obj:
            return False, f"missing required key: {key!r}"
    if schema.get("additionalProperties") is False:
        extra = set(obj) - set(props)
        if extra:
            return False, f"unexpected keys: {sorted(extra)}"
    for key, spec in props.items():
        if key not in obj:
            continue
        enum = spec.get("enum")
        if enum is not None and obj[key] not in enum:
            return False, f"{key!r}={obj[key]!r} not in enum {enum}"
    return True, ""

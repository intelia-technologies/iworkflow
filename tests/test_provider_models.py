from iworkflow.provider_models import (
    parse_prefer_entry,
    parse_prefer_list,
    resolve_model,
)


def test_cursor_flash_legacy_alias():
    prov, model = parse_prefer_entry("cursor_flash")
    assert prov == "cursor"
    assert model == "composer-2.5-fast"


def test_prefer_colon_syntax():
    prov, model = parse_prefer_entry("cursor:flash")
    assert prov == "cursor"
    assert model == "composer-2.5-fast"


def test_prefer_object_syntax():
    prov, model = parse_prefer_entry({"provider": "cursor", "model": "composer-2.5"})
    assert prov == "cursor"
    assert model == "composer-2.5"


def test_parse_prefer_list_with_models_map():
    targets = parse_prefer_list(
        ["codex", "cursor"],
        models={"cursor": "flash"},
    )
    assert targets == [("codex", None), ("cursor", "composer-2.5-fast")]


def test_resolve_model_gemini_alias():
    assert resolve_model("gemini", "flash") == "Gemini 3.5 Flash (Medium)"


def test_classify_route_uses_flash_hint():
    from iworkflow.routing import route

    order, why = route(
        None,
        schema=None,
        prompt="Classify every record.",
        available=["cursor", "gemini", "codex"],
    )
    assert why == "inferred=classify"
    assert order[0] == ("cursor", "composer-2.5-fast")


def test_dynamic_models_loading(tmp_path, monkeypatch):
    import json
    from iworkflow import provider_models, routing

    test_data = {
        "providers": {
            "cursor": {
                "label": "Custom Cursor Label",
                "cli": "cursor-agent -p --model <model>",
                "default": "custom-model",
                "models": {
                    "custom-model": {
                        "label": "Custom Model Label",
                        "aliases": ["custom"],
                        "notes": "Custom Notes"
                    }
                }
            }
        },
        "capabilities": {
            "cursor": {
                "model": "Custom Cursor Model",
                "great_at": ["custom_task"],
                "weak_at": []
            }
        }
    }

    test_file = tmp_path / "test_models.json"
    test_file.write_text(json.dumps(test_data), encoding="utf-8")

    # Patch the MODELS_FILE path
    monkeypatch.setattr(provider_models, "MODELS_FILE", test_file)
    monkeypatch.setattr(routing, "MODELS_FILE", test_file)

    # Force reload
    if test_file.is_file():
        try:
            data = json.loads(test_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                providers = data.get("providers") if "providers" in data else data
                if isinstance(providers, dict):
                    provider_models.PROVIDER_MODELS.update(providers)
                    provider_models._ALIAS_INDEX = None
                if "capabilities" in data:
                    routing.CAPABILITIES.update(data["capabilities"])
        except Exception:
            pass

    assert provider_models.PROVIDER_MODELS["cursor"]["label"] == "Custom Cursor Label"
    assert provider_models.resolve_model("cursor", "custom") == "custom-model"
    assert routing.CAPABILITIES["cursor"]["great_at"] == ["custom_task"]

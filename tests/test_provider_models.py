from iworkflow.provider_models import (
    parse_prefer_entry,
    parse_prefer_list,
    resolve_model,
)


def test_cursor_flash_legacy_alias():
    # the -fast tier costs extra for the same model — every flash spelling
    # must resolve to the base composer-2.5
    prov, model = parse_prefer_entry("cursor_flash")
    assert prov == "cursor"
    assert model == "composer-2.5"


def test_prefer_colon_syntax():
    prov, model = parse_prefer_entry("cursor:flash")
    assert prov == "cursor"
    assert model == "composer-2.5"


def test_prefer_object_syntax():
    prov, model = parse_prefer_entry({"provider": "cursor", "model": "composer-2.5"})
    assert prov == "cursor"
    assert model == "composer-2.5"


def test_parse_prefer_list_with_models_map():
    targets = parse_prefer_list(
        ["codex", "cursor"],
        models={"cursor": "flash"},
    )
    assert targets == [("codex", None), ("cursor", "composer-2.5")]


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
    assert order[0] == ("cursor", "composer-2.5")


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
                    provider_models._merge_provider_models(providers)
                    provider_models._ALIAS_INDEX = None
                if "capabilities" in data:
                    routing.CAPABILITIES.update(data["capabilities"])
        except Exception:
            pass

    assert provider_models.PROVIDER_MODELS["cursor"]["label"] == "Custom Cursor Label"
    assert provider_models.resolve_model("cursor", "custom") == "custom-model"
    assert routing.CAPABILITIES["cursor"]["great_at"] == ["custom_task"]


def test_default_cap_matches_scarcity():
    from iworkflow.provider_models import default_cap
    assert default_cap("claude") == 1          # high scarcity → scarce shared pool
    assert default_cap("codex") == 2
    assert default_cap("gemini") == 2
    assert default_cap("cursor") == 2
    assert default_cap("unknown-provider") == 2  # no metadata → default tier


def test_default_timeout_profiles():
    from iworkflow.provider_models import default_timeout
    assert default_timeout("claude") == 600    # slow TUI cold-start + scarce
    assert default_timeout("gemini") == 420     # 1M-context sweeps need room
    assert default_timeout("codex") == 300
    assert default_timeout("cursor") == 150     # fast fan-out
    assert default_timeout("unknown-provider") is None


def test_provider_scarcity_tiers():
    from iworkflow.provider_models import provider_scarcity
    assert provider_scarcity("claude") == "high"
    assert provider_scarcity("cursor") == "medium"
    assert provider_scarcity("codex") == "low"
    assert provider_scarcity("unknown-provider") == "low"

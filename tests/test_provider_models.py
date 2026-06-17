from iworkflow.provider_models import (
    parse_prefer_entry,
    parse_prefer_list,
    resolve_model,
)


def test_cursor_flash_legacy_alias():
    prov, model = parse_prefer_entry("cursor_flash")
    assert prov == "cursor"
    assert model == "composer-2.5-flash"


def test_prefer_colon_syntax():
    prov, model = parse_prefer_entry("cursor:flash")
    assert prov == "cursor"
    assert model == "composer-2.5-flash"


def test_prefer_object_syntax():
    prov, model = parse_prefer_entry({"provider": "cursor", "model": "composer-2.5"})
    assert prov == "cursor"
    assert model == "composer-2.5"


def test_parse_prefer_list_with_models_map():
    targets = parse_prefer_list(
        ["codex", "cursor"],
        models={"cursor": "flash"},
    )
    assert targets == [("codex", None), ("cursor", "composer-2.5-flash")]


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
    assert order[0] == ("cursor", "composer-2.5-flash")

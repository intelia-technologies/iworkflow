from iworkflow.routing import infer_kind, route


SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {"verdict": {"enum": ["DONE"]}},
}


def test_infer_kind_covers_prompt_categories():
    assert infer_kind("x" * 30_001, None) == "sweep"
    assert infer_kind("Please audit this design.", None) == "audit"
    assert infer_kind("Draft an email to the team.", None) == "write"
    assert infer_kind("Implement the scheduler change.", None) == "implement"
    assert infer_kind("Classify every record.", None) == "classify"
    assert infer_kind("Return a compact answer.", SCHEMA) == "structured"
    assert infer_kind("Return a compact answer.", None) == "default"


def test_route_honors_explicit_role():
    order, why = route(
        "auditor",
        schema=None,
        prompt="implement the feature",
        available=["codex", "gemini", "claude"],
    )

    assert why == "role=auditor"
    assert order == ["gemini", "codex"]


def test_route_filters_unavailable_providers():
    order, why = route(
        "doer",
        schema=None,
        prompt="implement the feature",
        available=["gemini"],
    )

    assert why == "role=doer"
    assert order == ["gemini"]


def test_schema_only_prompt_routes_codex_first():
    order, why = route(
        None,
        schema=SCHEMA,
        prompt="Return a compact answer.",
        available=["gemini", "codex"],
    )

    assert why == "inferred=structured"
    assert order == ["codex", "gemini"]

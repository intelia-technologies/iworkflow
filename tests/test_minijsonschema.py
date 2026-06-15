from iworkflow.minijsonschema import validate


SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"enum": ["DONE", "EXHAUSTED"]},
        "summary": {},
    },
    "additionalProperties": False,
}


def test_validate_required_missing_returns_false():
    ok, why = validate({"summary": "missing status"}, SCHEMA)

    assert ok is False
    assert "missing required key" in why


def test_validate_enum_violation_returns_false():
    ok, why = validate({"status": "PENDING"}, SCHEMA)

    assert ok is False
    assert "not in enum" in why


def test_validate_rejects_extra_key_when_additional_properties_false():
    ok, why = validate({"status": "DONE", "extra": True}, SCHEMA)

    assert ok is False
    assert "unexpected keys" in why


def test_validate_happy_path_returns_true():
    ok, why = validate({"status": "DONE", "summary": "ok"}, SCHEMA)

    assert ok is True
    assert why == ""

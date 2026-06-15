import pytest

from iworkflow.providers import (
    Provider,
    ProviderError,
    RateLimited,
    _find_schema_json,
    _response_text,
)


SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary"],
    "properties": {
        "verdict": {"enum": ["DONE"]},
        "summary": {},
    },
    "additionalProperties": False,
}


def test_classify_does_not_rate_limit_successful_output_mentions_limits():
    Provider._classify(0, "this answer discusses rate limit and quota handling")


def test_classify_failed_limit_text_raises_rate_limited():
    with pytest.raises(RateLimited):
        Provider._classify(1, "usage limit reached")


def test_classify_failed_non_limit_text_raises_provider_error():
    with pytest.raises(ProviderError, match="exit 1"):
        Provider._classify(1, "boom")


def test_classify_timeout_raises_provider_error():
    with pytest.raises(ProviderError, match="timed out"):
        Provider._classify(124, "usage limit reached")


def test_find_schema_json_extracts_last_schema_valid_object():
    text = """
    noise {"verdict":"DONE","summary":"old"}
    later {"verdict":"BAD","summary":"wrong"}
    final {"verdict":"DONE","summary":"new"}
    trailing {"verdict":"DONE","summary":"extra","extra":true}
    """

    assert _find_schema_json(text, SCHEMA) == {
        "verdict": "DONE",
        "summary": "new",
    }


def test_response_text_strips_marker_and_tui_chrome():
    pane = """
    previous response
    ⏺
      Useful result
      ─────────────
      ❯
      plan mode on
      second line
    """

    assert _response_text(pane) == "Useful result\nsecond line"

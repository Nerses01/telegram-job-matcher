from unittest.mock import AsyncMock

import ai_engine


def test_parse_valid():
    assert ai_engine._parse_result('{"match_percentage": 85, "reason": "good fit"}') == {
        "match_percentage": 85,
        "reason": "good fit",
    }


def test_parse_string_percentage_coerced():
    assert ai_engine._parse_result('{"match_percentage": "90", "reason": "x"}')["match_percentage"] == 90


def test_parse_clamps_out_of_range():
    assert ai_engine._parse_result('{"match_percentage": 150, "reason": "x"}')["match_percentage"] == 100
    assert ai_engine._parse_result('{"match_percentage": -5, "reason": "x"}')["match_percentage"] == 0


def test_parse_missing_key_defaults_zero():
    result = ai_engine._parse_result('{"reason": "no score"}')
    assert result["match_percentage"] == 0
    assert result["reason"] == "no score"


def test_parse_non_numeric_percentage():
    assert ai_engine._parse_result('{"match_percentage": "abc"}')["match_percentage"] == 0


def test_parse_invalid_json():
    assert ai_engine._parse_result("not json at all") == {
        "match_percentage": 0,
        "reason": "Failed to parse AI output.",
    }


class _FakeResponse:
    def __init__(self, content):
        message = type("M", (), {"content": content})()
        choice = type("C", (), {"message": message})()
        self.choices = [choice]


async def test_evaluate_happy_path(monkeypatch):
    monkeypatch.setattr(
        ai_engine.client.chat.completions,
        "create",
        AsyncMock(return_value=_FakeResponse('{"match_percentage": 70, "reason": "ok"}')),
    )
    result = await ai_engine.evaluate_job_match("cv", "job")
    assert result == {"match_percentage": 70, "reason": "ok"}


async def test_evaluate_non_retryable_error_falls_back(monkeypatch):
    monkeypatch.setattr(
        ai_engine.client.chat.completions,
        "create",
        AsyncMock(side_effect=ValueError("boom")),
    )
    result = await ai_engine.evaluate_job_match("cv", "job")
    assert result["match_percentage"] == 0
    assert "API Error" in result["reason"]

import json
from unittest.mock import AsyncMock

import pytest

import ai_engine


def test_parse_valid():
    assert ai_engine._parse_result('{"match_percentage": 85, "reason": "good fit"}') == {
        "match_percentage": 85,
        "reason": "good fit",
        "work_mode": "unknown",
        "matched_skills": [],
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
    # The failure path must still carry the new keys so callers never KeyError.
    assert ai_engine._parse_result("not json at all") == {
        "match_percentage": 0,
        "reason": "Failed to parse AI output.",
        "work_mode": "unknown",
        "matched_skills": [],
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
        AsyncMock(return_value=_FakeResponse(
            '{"match_percentage": 70, "reason": "ok", "work_mode": "remote",'
            ' "matched_skills": ["Python"]}'
        )),
    )
    result = await ai_engine.evaluate_job_match("cv", "job")
    assert result == {
        "match_percentage": 70,
        "reason": "ok",
        "work_mode": "remote",
        "matched_skills": ["Python"],
    }


async def test_evaluate_non_retryable_error_falls_back(monkeypatch):
    monkeypatch.setattr(
        ai_engine.client.chat.completions,
        "create",
        AsyncMock(side_effect=ValueError("boom")),
    )
    result = await ai_engine.evaluate_job_match("cv", "job")
    assert result["match_percentage"] == 0
    assert "API Error" in result["reason"]
    # Must fail open on work mode, never suppress a job because the API died.
    assert result["work_mode"] == "unknown"
    assert result["matched_skills"] == []


# --- work_mode normalization -------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"work_mode": "remote"}', "remote"),
    ('{"work_mode": "Remote "}', "remote"),
    ('{"work_mode": "OFFICE"}', "office"),
    ('{"work_mode": "hybrid"}', "hybrid"),
    ('{"work_mode": "on-site"}', "unknown"),   # not a recognized token
    ('{"work_mode": null}', "unknown"),
    ('{"work_mode": 123}', "unknown"),
    ('{}', "unknown"),                          # missing key
])
def test_work_mode_normalization(raw, expected):
    assert ai_engine._parse_result(raw)["work_mode"] == expected


# --- matched_skills coercion -------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"matched_skills": ["Python", "Go"]}', ["Python", "Go"]),
    ('{"matched_skills": "Python"}', []),        # not a list
    ('{"matched_skills": null}', []),
    ('{"matched_skills": ["Python", "python"]}', ["Python"]),  # case-insensitive dedupe
    ('{}', []),
])
def test_matched_skills_coercion(raw, expected):
    assert ai_engine._parse_result(raw)["matched_skills"] == expected


def test_matched_skills_capped():
    many = [f"skill{i}" for i in range(50)]
    result = ai_engine._parse_result(json.dumps({"matched_skills": many}))
    assert len(result["matched_skills"]) <= ai_engine.config.MAX_MATCHED_SKILLS_IN_ALERT


# --- _normalize_skills -------------------------------------------------------

def test_normalize_skills_cleans_and_dedupes():
    raw = ["  Python  ", "python", "PostgreSQL", "", "   ", "Go,", "a" * 100, 42]
    result = ai_engine._normalize_skills(raw)
    assert result == ["Python", "PostgreSQL", "Go", "42"]


def test_normalize_skills_non_list():
    assert ai_engine._normalize_skills("Python") == []
    assert ai_engine._normalize_skills(None) == []


def test_normalize_skills_caps_at_max():
    result = ai_engine._normalize_skills([f"s{i}" for i in range(200)])
    assert len(result) == ai_engine.config.MAX_SKILLS


# --- extract_skills_from_cv --------------------------------------------------

async def test_extract_skills_happy_path(monkeypatch):
    monkeypatch.setattr(
        ai_engine.client.chat.completions,
        "create",
        AsyncMock(return_value=_FakeResponse('{"skills": ["Python", "Docker"]}')),
    )
    assert await ai_engine.extract_skills_from_cv("my cv") == ["Python", "Docker"]


async def test_extract_skills_malformed_json(monkeypatch):
    monkeypatch.setattr(
        ai_engine.client.chat.completions,
        "create",
        AsyncMock(return_value=_FakeResponse("not json")),
    )
    assert await ai_engine.extract_skills_from_cv("my cv") == []


async def test_extract_skills_api_error(monkeypatch):
    monkeypatch.setattr(
        ai_engine.client.chat.completions,
        "create",
        AsyncMock(side_effect=ValueError("boom")),
    )
    # Must never raise: a Groq outage cannot be allowed to block saving a CV.
    assert await ai_engine.extract_skills_from_cv("my cv") == []


async def test_extract_skills_empty_cv():
    assert await ai_engine.extract_skills_from_cv("   ") == []

"""LLM contract: canonicalisation, validation, JSON extraction and repair (LLM-2/6)."""

from __future__ import annotations

import pytest

from app.services.llm import (
    LLMError,
    LLMResult,
    ProviderConfig,
    canonicalise,
    extract_json,
    generate_resume,
    validate_resume,
)


def test_canonicalise_maps_legacy_aliases():
    legacy = {
        "name": "Ada Lovelace",
        "title": "Engineer",
        "target_company": "",
        "summary": "Summary",
        "contact": {"email": "ada@example.com", "phone": "123", "linkedin": "in/ada"},
        "skills": [{"category": "Languages", "items": ["Python", "SQL"]}],
        "experience": [{"role": "Engineer", "company": "Acme", "duration": "2020-2024", "bullets": ["did things"]}],
        "education": [{"field_of_study": "CS", "university": "MIT"}],
        "company_name": "Acme Corp",
    }
    out = canonicalise(legacy)
    assert out["email"] == "ada@example.com"
    assert out["technicalskills"] == {"Languages": "Python, SQL"}
    assert out["experience"][0]["title"] == "Engineer"
    assert out["experience"][0]["dates"] == "2020-2024"
    assert out["education"][0]["degree"] == "CS"
    assert out["education"][0]["school"] == "MIT"
    assert out["target_company"] == "Acme Corp"
    assert validate_resume(out) == []


def test_validate_reports_missing_required_fields():
    errors = validate_resume({"name": "", "experience": []})
    assert any("title" in error for error in errors)
    assert any("experience" in error for error in errors)


def test_extract_json_handles_markdown_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(LLMError):
        extract_json("not json at all")


class ScriptedClient:
    """Returns the queued payloads in order; records the calls it received."""

    def __init__(self, payloads: list[str]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        self.calls.append(user)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return LLMResult(json={}, raw=payload, usage={"tokens_in": 1, "tokens_out": 1, "tokens_cached": 0}, latency_ms=5)


VALID_RESUME = (
    '{"name":"Ada","title":"Engineer","target_company":"Acme","summary":"s",'
    '"experience":[{"title":"Engineer","company":"Acme","bullets":["b"]}]}'
)


@pytest.mark.asyncio
async def test_generate_resume_repairs_invalid_json_without_extra_attempts():
    client = ScriptedClient(["not json", VALID_RESUME])
    data, raw, log = await generate_resume(
        client, provider=ProviderConfig(id=None, type="mock", display_name="m", model="m"),
        profile_prompt="prompt", jd_text="jd",
    )
    assert data["name"] == "Ada"
    assert len(client.calls) == 2
    assert log[0]["error"] == "invalid_json"
    assert log[-1].get("errors") == []


@pytest.mark.asyncio
async def test_generate_resume_raises_after_two_repair_rounds():
    client = ScriptedClient(["nope"])
    with pytest.raises(LLMError) as excinfo:
        await generate_resume(
            client, provider=ProviderConfig(id=None, type="mock", display_name="m", model="m"),
            profile_prompt="", jd_text="jd",
        )
    assert excinfo.value.code == "invalid_json"
    assert len(client.calls) == 3


def test_anthropic_system_prompt_is_cache_marked():
    """CONC-5: the profile prompt is the cached, first message; the JD stays last."""
    from app.services.llm import build_messages

    provider = ProviderConfig(
        id="p1", type="anthropic", display_name="Anthropic", model="claude-sonnet-4-5",
        api_key="sk", base_url=None, timeout_s=30, max_concurrency=4, rpm=60,
        temperature=0.2, max_tokens=1000,
    )
    messages = build_messages(provider, "PROFILE PROMPT", "JOB DESCRIPTION")
    assert messages[0]["role"] == "system"
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[0]["content"][0]["text"] == "PROFILE PROMPT"
    assert messages[1] == {"role": "user", "content": "JOB DESCRIPTION"}


def test_openai_messages_stay_plain():
    from app.services.llm import build_messages

    provider = ProviderConfig(
        id="p2", type="openai", display_name="OpenAI", model="gpt-4o",
        api_key="sk", base_url=None, timeout_s=30, max_concurrency=4, rpm=60,
        temperature=0.2, max_tokens=1000,
    )
    assert build_messages(provider, "SYS", "USER") == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]

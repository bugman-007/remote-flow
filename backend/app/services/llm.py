"""LLM access: output contract, canonicalisation, validation, repair, providers."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.config import get_settings
from app.services.crypto import decrypt_secret

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "resume.schema.json"

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 8000
REPAIR_ROUNDS = 2

LEGACY_CONTACT_KEYS = ("email", "phone", "location", "citizenship", "work_authorization", "linkedin")

#: Appendix B rule 1-5 - appended to every Profile prompt by the system (LLM-1).
OUTPUT_CONTRACT = """\
You must answer with a single JSON object that validates against the Remote Flow resume schema.

1. Return ONLY the JSON object. No Markdown fences, no commentary, no trailing text.
2. Mark important terms with explicit spans: [highlight]Node.js[/highlight]. Never use the
   suffix form (Node.js[highlight]) and never use Markdown bold (**Node.js**).
3. Do not use Markdown links. Write plain URLs or plain text.
4. "education" is always an array of objects (never a single object).
5. Do not include a "job_description" field. The system injects the original job description.

Schema summary (all strings unless noted):
  name (required), title (required), subtitle,
  email, phone, location, citizenship, work_authorization, linkedin,
  summary (required),
  technicalskills: { "<Category>": "comma-separated text" },
  additional_information: { "<Label>": "text" },
  experience: [ { title (required), company (required), location, dates, bullets: [string, >=1] } ] (required, >=1),
  education: [ { degree, school, location, dates, gpa } ],
  target_company (required - the company from the job description),
  job_link,
  company_information: { public_facts, safe_inferences, research_limitations }
"""


@dataclass
class ProviderConfig:
    id: str | None
    type: str
    display_name: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    timeout_s: int = 600
    max_concurrency: int = 8
    rpm: int = 60
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    json: dict
    raw: str
    usage: dict[str, int]
    latency_ms: int
    finish_reason: str | None = None


class LLMError(Exception):
    def __init__(self, code: str, message: str, *, provider_error: bool = False, retry_after_s: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_error = provider_error
        self.retry_after_s = retry_after_s


class LLMClient(Protocol):
    async def generate_json(
        self,
        *,
        provider: ProviderConfig,
        system: str,
        user: str,
        json_schema: dict,
        timeout_s: int,
    ) -> LLMResult: ...


# --------------------------------------------------------------- canonicalising


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def canonicalise(data: Any) -> dict:
    """LLM-6: normalise legacy aliases into the canonical Appendix B shape."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {key: value for key, value in data.items() if key != "contact"}
    contact = data.get("contact")
    if isinstance(contact, dict):
        for key in LEGACY_CONTACT_KEYS:
            if not out.get(key) and contact.get(key):
                out[key] = contact[key]

    skills = data.get("skills")
    if not out.get("technicalskills") and isinstance(skills, list):
        merged: dict[str, str] = {}
        for entry in skills:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category") or entry.get("name") or "").strip()
            if not category:
                continue
            items = entry.get("items")
            if isinstance(items, list):
                merged[category] = ", ".join(str(item) for item in items)
            else:
                merged[category] = str(items or "").strip()
        if merged:
            out["technicalskills"] = merged
    if isinstance(out.get("experience"), list):
        for item in out["experience"]:
            if not isinstance(item, dict):
                continue
            if not item.get("title") and item.get("role"):
                item["title"] = item["role"]
            if not item.get("dates") and item.get("duration"):
                item["dates"] = item["duration"]
    education = out.get("education")
    if isinstance(education, dict):
        education = [education]
    if isinstance(education, list):
        for item in education:
            if not isinstance(item, dict):
                continue
            if not item.get("degree") and item.get("field_of_study"):
                item["degree"] = item["field_of_study"]
            if not item.get("school") and item.get("university"):
                item["school"] = item["university"]
        out["education"] = education
    if not out.get("target_company"):
        for alias in ("company_name", "company"):
            if isinstance(data.get(alias), str) and data[alias].strip():
                out["target_company"] = data[alias]
                break
    return out


def validate_resume(data: Any) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["response is not a JSON object"]
    for field in ("name", "title", "target_company", "summary"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} is required and must be a non-empty string")
    experience = data.get("experience")
    if not isinstance(experience, list) or not experience:
        errors.append("experience is required and must be a non-empty array")
    else:
        for index, item in enumerate(experience):
            if not isinstance(item, dict):
                errors.append(f"experience[{index}] must be an object")
                continue
            for field in ("title", "company"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f"experience[{index}].{field} is required")
            bullets = item.get("bullets")
            if not isinstance(bullets, list) or not bullets or not all(str(b).strip() for b in bullets):
                errors.append(f"experience[{index}].bullets must be a non-empty array of strings")
    education = data.get("education")
    if education is not None and not isinstance(education, list):
        errors.append("education must be an array")
    for key in ("technicalskills", "additional_information"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            errors.append(f"{key} must be an object")
    return errors


def extract_json(raw: str) -> dict:
    """Parse a model response that may be wrapped in Markdown fences."""
    if not raw or not raw.strip():
        raise LLMError("invalid_json", "empty response from provider")
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("invalid_json", "response contained no JSON object") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("invalid_json", f"response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("invalid_json", "response JSON is not an object")
    return parsed


def build_system_prompt(profile_prompt: str) -> str:
    """LLM-1: Profile prompt + the developer-maintained output contract."""
    return f"{profile_prompt.strip()}\n\n---\n\n{OUTPUT_CONTRACT}"


def inject_job_description(data: dict, jd_text: str) -> dict:
    """LLM-6: the original JD is injected by the system, never produced by the model."""
    out = dict(data)
    if jd_text and not str(out.get("job_description") or "").strip():
        out["job_description"] = jd_text
    return out


async def generate_resume(
    client: LLMClient,
    *,
    provider: ProviderConfig,
    profile_prompt: str,
    jd_text: str,
    schema: dict | None = None,
) -> tuple[dict, str, list[dict]]:
    """Call the model, repairing invalid JSON up to ``REPAIR_ROUNDS`` times.

    Returns ``(canonical_json, raw_response, attempt_log)``. Provider errors
    raise ``LLMError`` immediately - they are retried by the pipeline, not here.
    """
    schema = schema or load_schema()
    system = build_system_prompt(profile_prompt)
    user = jd_text
    log: list[dict] = []
    last_raw = ""
    for round_no in range(REPAIR_ROUNDS + 1):
        started = time.perf_counter()
        result = await client.generate_json(
            provider=provider, system=system, user=user, json_schema=schema, timeout_s=provider.timeout_s
        )
        last_raw = result.raw
        latency_ms = int(result.latency_ms or (time.perf_counter() - started) * 1000)
        try:
            parsed = extract_json(result.raw)
        except LLMError as exc:
            log.append({"round": round_no, "latency_ms": latency_ms, "error": exc.code, "repair": round_no < REPAIR_ROUNDS})
            if round_no >= REPAIR_ROUNDS:
                raise LLMError("invalid_json", f"model did not return JSON after {REPAIR_ROUNDS + 1} attempts") from exc
            user = (
                f"{jd_text}\n\n---\nYour previous reply was not valid JSON ({exc.message}). "
                "Reply again with only the JSON object described in the system prompt."
            )
            continue
        canonical = canonicalise(parsed)
        errors = validate_resume(canonical)
        log.append(
            {
                "round": round_no,
                "latency_ms": latency_ms,
                "usage": result.usage,
                "errors": errors,
                "repair": bool(errors) and round_no < REPAIR_ROUNDS,
            }
        )
        if not errors:
            return canonical, result.raw, log
        if round_no >= REPAIR_ROUNDS:
            raise LLMError("invalid_schema", "resume JSON failed validation: " + "; ".join(errors))
        user = (
            f"{jd_text}\n\n---\nYour previous JSON failed schema validation: {'; '.join(errors)}. "
            "Reply again with only the corrected JSON object."
        )
    raise LLMError("invalid_json", f"could not obtain valid JSON; last raw output was: {last_raw[:200]}")


# ------------------------------------------------------------------- providers


def provider_config_from_row(row, *, model: str | None = None, temperature: float | None = None,
                             max_tokens: int | None = None) -> ProviderConfig:
    try:
        api_key = decrypt_secret(row.api_key_enc)
    except Exception as exc:  # noqa: BLE001 - wrong MASTER_KEY
        raise LLMError("provider_key_undecryptable", f"cannot decrypt API key: {exc}", provider_error=True) from exc
    return ProviderConfig(
        id=row.id,
        type=row.type,
        display_name=row.display_name,
        model=model or row.default_model or "",
        api_key=api_key,
        base_url=row.base_url,
        timeout_s=row.timeout_s,
        max_concurrency=row.max_concurrency,
        rpm=row.rpm,
        temperature=temperature if temperature is not None else DEFAULT_TEMPERATURE,
        max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
    )


def build_messages(provider: ProviderConfig, system: str, user: str) -> list[dict]:
    """CONC-5: the long Profile prompt goes first and unchanged (cached when supported);
    the job description goes last."""
    if provider.type == "anthropic":
        return [
            {
                "role": "system",
                "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            },
            {"role": "user", "content": user},
        ]
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def litellm_model_name(provider_type: str, model: str, base_url: str | None = None) -> str:
    """Add the LiteLLM routing prefix a provider needs when the Manager left it off.

    ``gemini-2.5-flash`` on its own makes LiteLLM guess a provider and fail with a
    confusing import error; ``gemini/gemini-2.5-flash`` routes to Google correctly.
    """
    name = (model or "").strip()
    if not name or "/" in name:
        return name
    if provider_type == "google_gemini":
        return f"gemini/{name}"
    if provider_type == "openai_compatible" and base_url:
        return f"openai/{name}"
    return name


#: OpenAI-compatible hosts that reject ``json_schema`` and only accept ``json_object``.
JSON_OBJECT_HOSTS: tuple[str, ...] = ("deepseek.com",)
_RESUME_SCHEMA_NAME = "resume"


def json_schema_format(json_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": _RESUME_SCHEMA_NAME, "schema": json_schema, "strict": False},
    }


def json_response_format(provider: ProviderConfig, json_schema: dict[str, Any]) -> dict[str, Any]:
    """GEN-3: pick a JSON mode the provider actually supports.

    DeepSeek (and a few other OpenAI-compatible gateways) answer ``json_schema``
    with "This response_format type is unavailable now", so ask for plain
    ``json_object`` there and let the schema repair loop do the validating.
    """
    host = (provider.base_url or "").lower()
    if provider.type == "openai_compatible" and any(name in host for name in JSON_OBJECT_HOSTS):
        return {"type": "json_object"}
    return json_schema_format(json_schema)


def is_response_format_error(exc: Exception) -> bool:
    return "response_format" in str(exc).lower()


class LiteLLMClient:
    """Thin LiteLLM adapter (library, not proxy). Imported lazily."""

    async def generate_json(self, *, provider: ProviderConfig, system: str, user: str,
                            json_schema: dict, timeout_s: int) -> LLMResult:
        import litellm

        model = litellm_model_name(provider.type, provider.model, provider.base_url)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": build_messages(provider, system, user),
            "temperature": provider.temperature,
            "max_tokens": provider.max_tokens,
            "timeout": timeout_s,
            "api_key": provider.api_key,
        }
        if provider.base_url:
            kwargs["api_base"] = provider.base_url
        kwargs["response_format"] = json_response_format(provider, json_schema)
        kwargs.update(provider.params or {})
        started = time.perf_counter()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped to a pipeline error code
            # Any other gateway that refuses json_schema still gets a JSON answer.
            if kwargs["response_format"].get("type") == "json_schema" and is_response_format_error(exc):
                kwargs["response_format"] = {"type": "json_object"}
                try:
                    response = await litellm.acompletion(**kwargs)
                except Exception as second:  # noqa: BLE001
                    raise map_provider_exception(second) from second
            else:
                raise map_provider_exception(exc) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        if isinstance(details, dict):
            cached_tokens = int(details.get("cached_tokens", 0) or 0)
        elif details is not None:
            cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        else:
            cached_tokens = 0
        usage_dict = {
            "tokens_in": int(getattr(usage, "prompt_tokens", 0) or 0),
            "tokens_out": int(getattr(usage, "completion_tokens", 0) or 0),
            "tokens_cached": cached_tokens,
        }
        return LLMResult(
            json={},
            raw=raw,
            usage=usage_dict,
            latency_ms=latency_ms,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
        )


def map_provider_exception(exc: Exception) -> LLMError:
    text = str(exc)
    lowered = text.lower()
    status = getattr(exc, "status_code", None)
    retry_after = None
    for match in re.finditer(r"retry[- ]after[^0-9]*(\d+)", lowered):
        retry_after = int(match.group(1))
    if status == 429 or "rate limit" in lowered or "429" in lowered:
        return LLMError("provider_rate_limited", text[:500], provider_error=True, retry_after_s=retry_after)
    if status in (401, 403) or "unauthorized" in lowered or "invalid api key" in lowered:
        return LLMError("provider_auth_error", text[:500], provider_error=True)
    if status and int(status) >= 500:
        return LLMError("provider_server_error", text[:500], provider_error=True)
    if "timeout" in lowered or "timed out" in lowered:
        return LLMError("provider_timeout", text[:500], provider_error=True)
    return LLMError("provider_error", text[:500], provider_error=True)


MOCK_NAMES = ("Mock Candidate", "Taylor Reed", "Jordan Alvarez", "Sam Okafor")


class MockClient:
    """OPS-7: fixture JSON after a random delay with configurable failure rates."""

    def __init__(self, *, fail_rate: float | None = None, delay_ms: tuple[int, int] | None = None) -> None:
        settings = get_settings()
        self.fail_rate = settings.mock_llm_fail_rate if fail_rate is None else fail_rate
        self.delay_ms = delay_ms or (settings.mock_llm_delay_ms_min, settings.mock_llm_delay_ms_max)

    async def generate_json(self, *, provider: ProviderConfig, system: str, user: str,
                            json_schema: dict, timeout_s: int) -> LLMResult:
        import asyncio

        low, high = self.delay_ms
        await asyncio.sleep(random.randint(low, high) / 1000)
        if random.random() < self.fail_rate:
            raise LLMError("provider_rate_limited", "mock provider: injected failure", provider_error=True, retry_after_s=1)
        payload = mock_resume(user)
        return LLMResult(
            json=payload,
            raw=json.dumps(payload),
            usage={"tokens_in": 800, "tokens_out": 900, "tokens_cached": 0},
            latency_ms=random.randint(low, high),
            finish_reason="stop",
        )


def mock_resume(jd_text: str) -> dict:
    company = "Sample Company"
    match = re.search(r"(?:at|join|with)\s+([A-Z][A-Za-z0-9&.\- ]{2,40})", jd_text or "")
    if match:
        company = match.group(1).strip().rstrip(".,")
    seed = abs(hash(jd_text or "")) % len(MOCK_NAMES)
    return {
        "name": MOCK_NAMES[seed],
        "title": "Senior Full Stack Developer",
        "subtitle": "Senior Full Stack Developer · Remote",
        "email": "candidate@example.com",
        "phone": "+1 555 0100",
        "location": "Remote",
        "summary": "Full stack engineer focused on [highlight]TypeScript[/highlight], APIs and data.",
        "technicalskills": {"Languages": "TypeScript, Python, SQL", "Cloud": "AWS, Docker"},
        "experience": [
            {
                "title": "Senior Engineer",
                "company": "Previous Corp",
                "location": "Remote",
                "dates": "2020 – Present",
                "bullets": ["Built [highlight]Node.js[/highlight] services handling 1M requests/day."],
            }
        ],
        "education": [{"degree": "BSc Computer Science", "school": "State University", "dates": "2014 – 2018"}],
        "target_company": company,
    }


_client_override: LLMClient | None = None


def set_client_override(client: LLMClient | None) -> None:
    global _client_override
    _client_override = client


def make_client(provider: ProviderConfig) -> LLMClient:
    if _client_override is not None:
        return _client_override
    if provider.type == "mock":
        return MockClient()
    return LiteLLMClient()

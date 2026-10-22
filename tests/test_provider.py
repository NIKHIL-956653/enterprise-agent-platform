"""
EAP-50: two providers behind one Protocol, chosen by one setting.

No test here spends a cent: the dispatch tests construct real client objects with fake
keys (both SDKs build offline), and the behaviour tests patch the SDK call itself. CI
proves the boundary; the live demo proves the bill.
"""

from types import SimpleNamespace

import anthropic as anthropic_sdk
import httpx
import pytest

from eap.core.config import get_settings
from eap.llm import anthropic as anthropic_module
from eap.llm import gemini as gemini_module
from eap.llm import provider
from eap.llm.anthropic import AnthropicLLM, get_anthropic_llm
from eap.llm.base import LLMError
from eap.llm.gemini import GeminiLLM


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch: pytest.MonkeyPatch):
    """Every factory and breaker here is lru_cached per process; tests must not share."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    for cached in (
        get_settings,
        gemini_module.get_llm,
        gemini_module._llm_breaker,
        gemini_module._embed_breaker,
        anthropic_module.get_anthropic_llm,
        anthropic_module._breaker,
    ):
        cached.cache_clear()
    yield
    for cached in (get_settings, gemini_module.get_llm, anthropic_module.get_anthropic_llm):
        cached.cache_clear()


def test_the_default_provider_is_gemini() -> None:
    assert isinstance(provider.get_llm(), GeminiLLM)


def test_the_setting_switches_to_anthropic(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()

    # The whole EAP-50 claim in one assert: same call site, different vendor, and no
    # agent, runner or workflow knows it happened.
    assert isinstance(provider.get_llm(), AnthropicLLM)


def test_an_unknown_provider_is_an_llm_error(monkeypatch) -> None:
    # Pydantic's Literal already rejects this at the env boundary, so reach the branch
    # directly - it exists for the day a refactor loosens the type.
    monkeypatch.setattr(provider, "get_settings", lambda: SimpleNamespace(llm_provider="openai"))
    with pytest.raises(LLMError):
        provider.get_llm()


def test_anthropic_without_a_key_fails_loudly(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(LLMError):
        get_anthropic_llm()


async def test_provider_failures_collapse_to_llm_error(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", model="m", timeout_seconds=5)

    async def _boom(**kwargs):
        raise RuntimeError("vendor detail that must never reach a caller")

    monkeypatch.setattr(llm._client.messages, "create", _boom)

    with pytest.raises(LLMError) as err:
        await llm.complete("hello")
    assert "vendor detail" not in str(err.value)  # the collapse is the feature


async def test_a_529_overload_is_retried(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", model="m", timeout_seconds=5)
    calls = []

    class Overloaded(Exception):
        status_code = 529

    async def _flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Overloaded()
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="recovered")])

    monkeypatch.setattr(llm._client.messages, "create", _flaky)

    assert await llm.complete("hello") == "recovered"
    assert len(calls) == 2  # one overload absorbed, no failed run


async def test_a_hung_call_is_retried_by_us_not_by_the_sdk(monkeypatch) -> None:
    # The live run that hung for 60s: the SDK's hidden retries spent the whole budget on
    # 30s attempts while ours never fired, because a timeout did not count as transient.
    llm = AnthropicLLM(api_key="k", model="m", timeout_seconds=5)
    assert llm._client.max_retries == 0  # one retry policy, ours
    calls = []

    async def _hung_once(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise anthropic_sdk.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="recovered")])

    monkeypatch.setattr(llm._client.messages, "create", _hung_once)

    assert await llm.complete("hello") == "recovered"
    assert len(calls) == 2


def test_gemini_counts_timeouts_and_drops_as_transient() -> None:
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    assert gemini_module._is_transient(httpx.ReadTimeout("slow", request=request))
    assert gemini_module._is_transient(httpx.ConnectError("refused", request=request))
    assert not gemini_module._is_transient(ValueError("a bug is not a blip"))


async def test_an_empty_response_is_an_error(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", model="m", timeout_seconds=5)

    async def _empty(**kwargs):
        return SimpleNamespace(content=[])

    monkeypatch.setattr(llm._client.messages, "create", _empty)

    # An empty completion looking like success is how silent data loss starts.
    with pytest.raises(LLMError):
        await llm.complete("hello")

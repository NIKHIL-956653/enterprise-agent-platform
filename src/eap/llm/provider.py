"""
Provider selection (EAP-50) and per-tenant keys (EAP-47).

One function stands between the platform and the model vendors: the runner asks for "the
LLM" with the tenant's slug, and this module answers with that tenant's client - their
provider, their key - or the platform default when the tenant brings neither. Adding
provider N+1 is a sibling module plus one branch here; agents, runner, workflows stay
untouched, which is the entire point of the eap.llm.base Protocol.

Resolution order, each step overridable per tenant through the SecretResolver:
    provider:  TENANT_<SLUG>_LLM_PROVIDER      else settings.llm_provider
    api key:   TENANT_<SLUG>_<provider's key>  else the platform key

Per-tenant clients are cached by key: a tenant's runs reuse one HTTP pool, and two
tenants that share a key share a client, which is what sharing a key means. Each distinct
key also gets its own circuit-breaker scope - a revoked key opens ITS circuit, not
everyone's. Imports stay inside branches so a deployment configured for one provider
never pays the import cost of the other's SDK.
"""

from functools import lru_cache
from hashlib import sha256

from eap.core.config import get_settings
from eap.llm.base import LLM, LLMError
from eap.llm.secrets import get_secret_resolver

# Each provider's key name - the same name in per-tenant secrets and in settings.
_KEY_NAMES = {"gemini": "GOOGLE_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _scope(api_key: str) -> str:
    # Breaker scope: stable per key but never the key itself - scopes are cache keys and
    # may surface in logs or debuggers. A fingerprint cannot spend money.
    return sha256(api_key.encode()).hexdigest()[:12]


def get_llm(tenant_slug: str | None = None) -> LLM:
    s = get_settings()
    provider = s.llm_provider
    tenant_key: str | None = None

    if tenant_slug:
        resolver = get_secret_resolver()
        override = resolver.get_secret(tenant_slug, "LLM_PROVIDER")
        if override:
            provider = override.lower()
        if provider not in _KEY_NAMES:
            raise LLMError(f"unknown llm_provider '{provider}' for tenant '{tenant_slug}'")
        tenant_key = resolver.get_secret(tenant_slug, _KEY_NAMES[provider])

    if provider == "gemini":
        if tenant_key:
            return _tenant_gemini(tenant_key)
        from eap.llm.gemini import get_llm as _gemini_llm

        return _gemini_llm()
    if provider == "anthropic":
        if tenant_key:
            return _tenant_anthropic(tenant_key)
        from eap.llm.anthropic import get_anthropic_llm

        return get_anthropic_llm()
    raise LLMError(f"unknown llm_provider '{provider}'")


@lru_cache
def _tenant_gemini(api_key: str) -> LLM:
    from eap.llm.gemini import GeminiLLM

    s = get_settings()
    return GeminiLLM(
        api_key=api_key,
        model=s.gemini_model,
        timeout_seconds=s.llm_timeout_seconds,
        breaker_scope=_scope(api_key),
    )


@lru_cache
def _tenant_anthropic(api_key: str) -> LLM:
    from eap.llm.anthropic import AnthropicLLM

    s = get_settings()
    return AnthropicLLM(
        api_key=api_key,
        model=s.anthropic_model,
        timeout_seconds=s.llm_timeout_seconds,
        breaker_scope=_scope(api_key),
    )

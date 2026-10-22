"""
EAP-47: per-tenant provider keys behind the SecretResolver protocol.

Same economics as test_provider.py: no test here spends a cent. Clients are constructed
with fake keys (both SDKs build offline); what is under test is the RESOLUTION - whose
key, whose provider, which cache, which breaker - never the network. The last test
drives one real run through Temporal with the SDK call patched: the slug-to-key chain
proven end to end, still for free.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory
from eap.core.temporal import get_temporal_client
from eap.llm import anthropic as anthropic_module
from eap.llm import gemini as gemini_module
from eap.llm import provider, secrets
from eap.llm.anthropic import AnthropicLLM, get_anthropic_llm
from eap.llm.base import LLMError
from eap.llm.gemini import GeminiLLM
from eap.llm.secrets import EnvSecretResolver
from tests.test_temporal_workflow import _execute, _seed_tenant


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch: pytest.MonkeyPatch):
    """Every factory, breaker, and resolver is lru_cached per process; tests must not share."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    caches = (
        get_settings,
        gemini_module.get_llm,
        gemini_module._llm_breaker,
        gemini_module._embed_breaker,
        anthropic_module.get_anthropic_llm,
        anthropic_module._breaker,
        provider._tenant_gemini,
        provider._tenant_anthropic,
        secrets.get_secret_resolver,
    )
    for cached in caches:
        cached.cache_clear()
    yield
    for cached in caches:
        cached.cache_clear()


class TestEnvSecretResolver:
    def test_reads_tenant_scoped_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ACME_ANTHROPIC_API_KEY", "sk-acme")
        assert EnvSecretResolver().get_secret("acme", "ANTHROPIC_API_KEY") == "sk-acme"

    def test_slug_is_uppercased_and_dashes_flattened(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ACME_CORP_GOOGLE_API_KEY", "g-acme")
        assert EnvSecretResolver().get_secret("acme-corp", "GOOGLE_API_KEY") == "g-acme"

    def test_missing_and_empty_both_mean_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert EnvSecretResolver().get_secret("ghost", "ANTHROPIC_API_KEY") is None
        monkeypatch.setenv("TENANT_GHOST_ANTHROPIC_API_KEY", "")
        assert EnvSecretResolver().get_secret("ghost", "ANTHROPIC_API_KEY") is None

    def test_unknown_backend_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRET_BACKEND", "carrier-pigeon")
        with pytest.raises(ValueError):
            secrets.get_secret_resolver()

    def test_the_dotenv_file_is_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # How the demo works: TENANT_* lines live in .env, which Settings never exports.
        monkeypatch.setattr(secrets, "_dotenv", lambda: {"TENANT_ACME_ANTHROPIC_API_KEY": "sk-file"})
        assert EnvSecretResolver().get_secret("acme", "ANTHROPIC_API_KEY") == "sk-file"

    def test_the_process_environment_beats_the_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secrets, "_dotenv", lambda: {"TENANT_ACME_ANTHROPIC_API_KEY": "sk-file"})
        monkeypatch.setenv("TENANT_ACME_ANTHROPIC_API_KEY", "sk-env")
        assert EnvSecretResolver().get_secret("acme", "ANTHROPIC_API_KEY") == "sk-env"

    def test_slugs_that_could_collide_get_no_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "acme_corp" and "Acme-Corp" both flatten onto acme-corp's variable. Neither may
        # read it: no secrets means platform defaults, which is safe - a neighbour's key
        # is a cross-tenant leak.
        monkeypatch.setenv("TENANT_ACME_CORP_ANTHROPIC_API_KEY", "sk-acme-corp")
        resolver = EnvSecretResolver()
        assert resolver.get_secret("acme-corp", "ANTHROPIC_API_KEY") == "sk-acme-corp"
        assert resolver.get_secret("acme_corp", "ANTHROPIC_API_KEY") is None
        assert resolver.get_secret("Acme-Corp", "ANTHROPIC_API_KEY") is None
        with pytest.raises(ValueError):
            secrets.env_name("acme_corp", "ANTHROPIC_API_KEY")


class TestPerTenantDispatch:
    def test_tenant_key_builds_tenant_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("TENANT_ACME_ANTHROPIC_API_KEY", "sk-acme-own")
        llm = provider.get_llm("acme")
        assert isinstance(llm, AnthropicLLM)
        assert llm._client.api_key == "sk-acme-own"

    def test_no_tenant_key_falls_back_to_platform_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert provider.get_llm("acme") is provider.get_llm()

    def test_tenant_provider_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Platform default is gemini; acme chose anthropic and brought their own key.
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("TENANT_ACME_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("TENANT_ACME_ANTHROPIC_API_KEY", "sk-acme-own")
        assert isinstance(provider.get_llm("acme"), AnthropicLLM)
        assert isinstance(provider.get_llm(), GeminiLLM)

    def test_provider_override_without_key_uses_platform_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("TENANT_ACME_LLM_PROVIDER", "anthropic")
        assert provider.get_llm("acme") is get_anthropic_llm()

    def test_unknown_tenant_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ACME_LLM_PROVIDER", "clippy")
        with pytest.raises(LLMError):
            provider.get_llm("acme")

    def test_clients_cached_per_key_not_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("TENANT_ACME_ANTHROPIC_API_KEY", "sk-acme-own")
        monkeypatch.setenv("TENANT_GLOBEX_ANTHROPIC_API_KEY", "sk-globex-own")
        assert provider.get_llm("acme") is provider.get_llm("acme")
        assert provider.get_llm("acme") is not provider.get_llm("globex")

    def test_breaker_scoped_per_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole point of the scope: acme's revoked key opens acme's circuit, not the
        # platform's. Different scopes, different breakers, independent failure counts.
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("TENANT_ACME_ANTHROPIC_API_KEY", "sk-acme-own")
        tenant_llm = provider.get_llm("acme")
        platform_llm = provider.get_llm()
        assert tenant_llm._breaker_scope != platform_llm._breaker_scope
        assert anthropic_module._breaker(tenant_llm._breaker_scope) is not anthropic_module._breaker(
            platform_llm._breaker_scope
        )


async def _slug_of(tenant_id: str) -> str:
    async with get_owner_session_factory()() as s:
        return (
            await s.execute(text("SELECT slug FROM tenants WHERE id = :tid"), {"tid": uuid.UUID(tenant_id)})
        ).scalar_one()


async def test_a_tenants_own_key_drives_their_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole chain, no network: worker -> runner -> tenant row (under RLS) -> slug ->
    # resolver -> the tenant's own client. Clients are cached per key, so the test holds the
    # exact objects the worker will be handed and patches their SDK calls.
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    tenant_id = await _seed_tenant()
    slug = await _slug_of(tenant_id)
    monkeypatch.setenv(secrets.env_name(slug, "ANTHROPIC_API_KEY"), "sk-tenant-own")

    tenant_calls: list[dict] = []
    platform_calls: list[dict] = []

    async def _tenant_reply(**kwargs):
        tenant_calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Done.")])

    async def _platform_reply(**kwargs):
        platform_calls.append(kwargs)
        raise RuntimeError("billed to the platform key")

    monkeypatch.setattr(provider.get_llm(slug)._client.messages, "create", _tenant_reply)
    monkeypatch.setattr(provider.get_llm()._client.messages, "create", _platform_reply)

    result = await _execute(await get_temporal_client(), tenant_id)

    assert platform_calls == [], "the run was billed to the platform key, not the tenant's"
    assert len(tenant_calls) == 1
    assert result.status == "succeeded"

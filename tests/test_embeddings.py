"""
The embedding boundary.

No network. The Protocol is exercised through a fake, and the only Gemini code tested
directly is the normalisation helper, which is pure.
"""

import math

import pytest

from eap.core.config import get_settings
from eap.llm.base import Embedder, LLMError
from eap.llm.gemini import _unit, get_embedder


class FakeEmbedder:
    """Deterministic stand-in — the same text always yields the same vector."""

    def __init__(self, dimensions: int = 8) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        return _unit([float(sum(text.encode()) % (i + 7) + 1) for i in range(self._dimensions)])


def test_the_fake_satisfies_the_protocol() -> None:
    # runtime_checkable only proves the methods exist — which is exactly the contract the
    # rest of the codebase relies on, since nothing imports GeminiEmbedder directly.
    assert isinstance(FakeEmbedder(), Embedder)


async def test_order_and_width_are_preserved() -> None:
    e = FakeEmbedder()
    vectors = await e.embed_documents(["alpha", "beta", "gamma"])

    assert len(vectors) == 3
    assert all(len(v) == e.dimensions for v in vectors)
    # Order is load-bearing: chunk i must receive vector i or retrieval returns the wrong text.
    assert vectors[0] == await e.embed_query("alpha")


async def test_empty_input_costs_no_call() -> None:
    assert await FakeEmbedder().embed_documents([]) == []


def test_unit_returns_unit_length() -> None:
    v = _unit([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0)
    assert math.isclose(v[0], 0.6)


def test_unit_rejects_a_zero_vector() -> None:
    with pytest.raises(LLMError):
        _unit([0.0, 0.0, 0.0])


def test_get_embedder_without_a_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Blanked explicitly rather than assumed empty — this must keep passing once a real
    # key lands in .env.
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    get_settings.cache_clear()
    get_embedder.cache_clear()
    try:
        with pytest.raises(LLMError):
            get_embedder()
    finally:
        get_embedder.cache_clear()

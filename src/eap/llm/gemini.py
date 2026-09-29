"""
The Gemini implementation of the LLM protocol.

This is the ONLY module in the codebase that imports Google's SDK. Everything else depends
on eap.llm.base.LLM, so adding another provider means adding a sibling file here — never
editing an agent.
"""

import math
from functools import lru_cache

from google import genai
from google.genai import types

from eap.core.config import get_settings
from eap.llm.base import LLMError


class GeminiLLM:
    """Wraps Google's client so the rest of the app never sees it."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int) -> None:
        self._client = genai.Client(
            api_key=api_key,
            # An HTTP call with no timeout can hang forever, and asyncio.timeout in the
            # runner cannot interrupt a socket that is simply waiting. The client needs its
            # own deadline. Google's option is in milliseconds.
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )
        self._model = model

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens,
                    # Low, not zero: summaries should be stable run to run so a failing
                    # test means the code changed, not that the model felt creative.
                    temperature=0.2,
                ),
            )
        except Exception as exc:
            # Every provider failure collapses to one error type. The caller gets "it
            # failed"; the original stays in the logs via `from exc` and never reaches an
            # HTTP response, where it would advertise what we run.
            raise LLMError("model call failed") from exc

        text = response.text
        if not text:
            # A blocked or empty response is still a 200 from Google's side. Treat it as a
            # failure rather than returning "" and letting an empty summary look like
            # success.
            raise LLMError("model returned no text")
        return text.strip()


@lru_cache
def get_llm() -> GeminiLLM:
    """
    One client per process — same reasoning as the database engine. It holds an HTTP
    connection pool, so building a fresh one per request throws away every warm connection.
    """
    s = get_settings()
    if not s.google_api_key:
        raise LLMError("google_api_key is not configured")
    return GeminiLLM(
        api_key=s.google_api_key,
        model=s.gemini_model,
        timeout_seconds=s.llm_timeout_seconds,
    )


class GeminiEmbedder:
    """
    Gemini's embedding endpoint behind eap.llm.base.Embedder.

    Vectors come back L2-normalised. Two reasons: output_dimensionality truncates a longer
    embedding from the end, and a truncated vector is no longer unit length, which quietly
    skews cosine distance; and normalising here means retrieval is a plain `<=>` with
    nothing to remember at the call site.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: int,
        batch_size: int,
    ) -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            out.extend(await self._embed(batch, "RETRIEVAL_DOCUMENT"))
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]

    async def _embed(self, batch: list[str], task_type: str) -> list[list[float]]:
        try:
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=batch,
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=self._dimensions,
                ),
            )
        except Exception as exc:
            raise LLMError("embedding call failed") from exc

        embeddings = response.embeddings or []
        if len(embeddings) != len(batch):
            # A short batch would misalign chunks and vectors — chunk 3 stored with chunk
            # 4's embedding — and retrieval would return confident nonsense with nothing
            # in the logs to explain it.
            raise LLMError("embedding count did not match input count")

        vectors = [_unit(list(e.values or [])) for e in embeddings]
        if any(len(v) != self._dimensions for v in vectors):
            # Fail here, not at the INSERT: pgvector rejects a wrong width with an opaque
            # error thousands of rows into an ingestion, long after the cause.
            raise LLMError("embedding dimension did not match configuration")
        return vectors


def _unit(vector: list[float]) -> list[float]:
    """L2-normalise. Rejects anything the vector column could not be searched with."""
    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude == 0.0:
        # A zero vector has no direction, so cosine distance to it is undefined — pgvector
        # yields NaN and the row silently never matches anything, forever.
        raise LLMError("embedding had zero magnitude")
    return [v / magnitude for v in vector]


@lru_cache
def get_embedder() -> GeminiEmbedder:
    """One client per process — same reasoning as get_llm()."""
    s = get_settings()
    if not s.google_api_key:
        raise LLMError("google_api_key is not configured")
    return GeminiEmbedder(
        api_key=s.google_api_key,
        model=s.embedding_model,
        dimensions=s.embedding_dimensions,
        timeout_seconds=s.llm_timeout_seconds,
        batch_size=s.embedding_batch_size,
    )

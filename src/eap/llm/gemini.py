"""
The Gemini implementation of the LLM protocol.

This is the ONLY module in the codebase that imports Google's SDK. Everything else depends
on eap.llm.base.LLM, so adding another provider means adding a sibling file here — never
editing an agent.
"""

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

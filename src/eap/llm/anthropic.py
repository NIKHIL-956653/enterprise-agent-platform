"""
The Anthropic implementation of the LLM protocol (EAP-50).

The second provider, and the proof of the boundary gemini.py promised: this file exists,
provider.get_llm() learned one branch, and not a single agent changed. Anthropic has no
embeddings API, so GeminiEmbedder keeps that half of the job whatever the setting says.

Same rule as gemini.py: this is the ONLY module that imports Anthropic's SDK.
"""

from functools import lru_cache

import anthropic

from eap.core.config import get_settings
from eap.llm.base import LLMError
from eap.llm.resilience import CircuitBreaker, CircuitOpenError, call_with_retry

# 529 is Anthropic's "overloaded" - transient by definition.
_TRANSIENT_CODES = {429, 500, 502, 503, 504, 529}


def _is_transient(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_CODES:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


@lru_cache
def _breaker() -> CircuitBreaker:
    # Anthropic gets its own breaker: Gemini being down must not slam this door, and the
    # reverse. Per provider, per endpoint, per process - same shape as gemini.py's pair.
    s = get_settings()
    return CircuitBreaker(
        failure_threshold=s.breaker_failure_threshold,
        cooldown_seconds=s.breaker_cooldown_seconds,
    )


class AnthropicLLM:
    """Wraps Anthropic's client so the rest of the app never sees it."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int) -> None:
        # The SDK takes its timeout in seconds and applies it per request - same reasoning
        # as gemini.py: asyncio.timeout upstream cannot interrupt a waiting socket.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)
        self._model = model

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        s = get_settings()

        async def _call():
            return await self._client.messages.create(
                model=self._model,
                max_tokens=max_output_tokens,
                # No temperature: the 2.x API dropped sampling knobs entirely - output
                # stability is the model's job now, not a parameter's. Gemini still takes
                # one; that difference lives HERE, behind the Protocol, where it belongs.
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            response = await _breaker().call(
                lambda: call_with_retry(
                    _call,
                    attempts=s.llm_retry_attempts,
                    base_delay_s=s.llm_retry_base_delay_ms / 1000,
                    is_transient=_is_transient,
                )
            )
        except CircuitOpenError:
            raise
        except Exception as exc:
            # Same collapse as Gemini: the caller learns "it failed", the original stays
            # in logs via `from exc`, and no HTTP response advertises what we run.
            raise LLMError("model call failed") from exc

        parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise LLMError("model returned no text")
        return text


@lru_cache
def get_anthropic_llm() -> AnthropicLLM:
    """One client per process - it holds the HTTP pool, same reasoning as get_llm()."""
    s = get_settings()
    if not s.anthropic_api_key:
        raise LLMError("anthropic_api_key is not configured")
    return AnthropicLLM(
        api_key=s.anthropic_api_key,
        model=s.anthropic_model,
        timeout_seconds=s.llm_timeout_seconds,
    )

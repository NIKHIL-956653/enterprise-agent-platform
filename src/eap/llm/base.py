"""
The LLM boundary.

Agents depend on these Protocols, not on a vendor SDK. That single indirection is what lets
tests run a deterministic fake and lets us swap Gemini for another provider without
touching a single agent.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """
    A model call failed.

    Deliberately carries no provider detail. Callers only ever need to know "it failed" —
    leaking the vendor's error text into an API response tells an attacker what we run.
    """


@runtime_checkable
class LLM(Protocol):
    """Anything that can turn a prompt into text."""

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str: ...


@dataclass(frozen=True)
class Completion:
    """A model's answer with what it cost: the provider's own token counts (EAP-64)."""

    text: str
    prompt_tokens: int
    completion_tokens: int


@runtime_checkable
class MeteredLLM(Protocol):
    """
    An LLM that also reports usage. Both real providers do; the runner prefers this method
    when it exists and falls back to `complete` plus an estimate when it does not, so a
    test fake that only knows `complete` keeps working - and still gets counted.
    """

    async def complete_metered(self, prompt: str, *, max_output_tokens: int = 1024) -> Completion: ...


# Roughly four characters per token for English prose on every current tokenizer. Only used
# for a model that reports no usage; the two real providers do. Never zero for a non-empty
# text, because a budget that counts nothing for an unmetered call is no budget.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN if text else 0


@runtime_checkable
class Embedder(Protocol):
    """
    Anything that can turn text into vectors.

    Two methods rather than one because retrieval is asymmetric: a stored passage and the
    question asked about it are embedded for different jobs, and providers expose that as a
    task hint. Two names keep the vendor's vocabulary out of the callers while still using it.
    """

    @property
    def dimensions(self) -> int:
        """Width of every vector returned. Must match the vector(N) column exactly."""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages for storage. Output order matches the input exactly."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed one question for searching."""
        ...

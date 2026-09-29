"""
The LLM boundary.

Agents depend on these Protocols, not on a vendor SDK. That single indirection is what lets
tests run a deterministic fake and lets us swap Gemini for another provider without
touching a single agent.
"""

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

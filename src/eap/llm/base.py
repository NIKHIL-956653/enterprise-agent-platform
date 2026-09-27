"""
The LLM boundary.

Agents depend on this Protocol, not on a vendor SDK. That single indirection is what lets
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

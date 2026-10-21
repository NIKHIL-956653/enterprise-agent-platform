"""
Provider selection (EAP-50).

One function stands between the platform and the model vendors: the runner asks for "the
LLM", this module reads one setting and answers. Adding provider N+1 is a sibling module
plus one branch here - agents, runner, workflows stay untouched, which is the entire
point of the eap.llm.base Protocol.

Imports are inside the branches on purpose: a deployment configured for one provider
never pays the import cost - or the packaging requirement - of the other's SDK beyond
what the lockstep dependency list already carries.
"""

from eap.core.config import get_settings
from eap.llm.base import LLM, LLMError


def get_llm() -> LLM:
    provider = get_settings().llm_provider
    if provider == "gemini":
        from eap.llm.gemini import get_llm as _gemini_llm

        return _gemini_llm()
    if provider == "anthropic":
        from eap.llm.anthropic import get_anthropic_llm

        return get_anthropic_llm()
    raise LLMError(f"unknown llm_provider '{provider}'")

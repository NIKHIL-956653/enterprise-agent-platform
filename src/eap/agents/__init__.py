"""
Agent package.

Importing an agent module is what registers it. Every new agent gets a line below - the
same rule as models/__init__.py, and the same failure mode if you forget: the agent simply
does not exist at dispatch.
"""

from eap.agents import echo  # noqa: E402,F401  (import for its registration side effect)
from eap.agents.base import AgentContext, AgentError, BaseAgent
from eap.agents.registry import get_agent, register, registered_names

__all__ = [
    "AgentContext",
    "AgentError",
    "BaseAgent",
    "get_agent",
    "register",
    "registered_names",
]

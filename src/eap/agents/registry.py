"""
Agent registry.

Agents are platform-authored code, discovered at import time - not tenant-supplied and
never executed from the database. The agents table records only that a tenant has ENABLED
an agent; the implementation is always this registry.

That separation is the security model: a row in the database can never introduce new code.

This lives in its own module so agent modules can import `register` without importing the
package __init__ that imports them - which would be circular.
"""

from eap.agents.base import AgentError, BaseAgent

_REGISTRY: dict[str, type[BaseAgent]] = {}


def register(cls: type[BaseAgent]) -> type[BaseAgent]:
    """Class decorator. Duplicate names are a bug, so they raise rather than overwrite."""
    if cls.name in _REGISTRY:
        raise ValueError(f"agent '{cls.name}' is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def get_agent(name: str) -> type[BaseAgent]:
    """Look up an implementation, or raise. Called at dispatch, before any work starts."""
    try:
        return _REGISTRY[name]
    except KeyError as e:
        raise AgentError(f"no agent implementation registered as '{name}'") from e


def registered_names() -> list[str]:
    return sorted(_REGISTRY)

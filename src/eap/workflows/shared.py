"""
Payloads that cross the Temporal boundary.

Stdlib only, deliberately: workflow code runs inside Temporal's sandbox, which replays it
deterministically and restricts what it may import. A dataclass of strings and ints is
safe there; SQLAlchemy models and UUID-typed fields are not worth the fight. Everything is
serialised into workflow history, so these are also a WIRE FORMAT - rename a field and
in-flight workflows fail to decode. Grow them carefully.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentRunRequest:
    tenant_id: str
    user_id: str | None
    clearance: int
    agent_name: str
    payload: dict[str, Any]


@dataclass
class AgentRunResult:
    run_id: str
    status: str
    error: str | None

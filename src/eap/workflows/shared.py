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
    # The pre-created run row to execute against (EAP-44). The API creates the row with
    # status QUEUED before enqueueing, so the caller holds an id to poll before any worker
    # picks the job up. Optional so histories serialized before this field still decode.
    run_id: str | None = None


@dataclass
class AgentRunResult:
    run_id: str
    status: str
    error: str | None
    # EAP-44: the sync API path answers from this result instead of re-reading the row,
    # so the result carries everything the response envelope needs.
    output: dict[str, Any] | None = None
    latency_ms: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

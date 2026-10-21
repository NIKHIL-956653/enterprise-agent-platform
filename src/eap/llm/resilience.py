"""
Resilience for outbound provider calls: bounded retry with jitter, and a circuit breaker.

SDK-agnostic on purpose - gemini.py stays the only module that imports Google's SDK, so
this one takes an `is_transient` callable instead of knowing any provider's exceptions.

The split of duties: retry absorbs a DROPPED DELIVERY (one 429, one timeout) without
failing the run; the breaker absorbs a BURNING WAREHOUSE (the provider is down) without
burning our own latency budget - after enough consecutive failures every call fails in
microseconds instead of waiting out a timeout, and one probe per cooldown checks whether
the provider has recovered.

Both are per worker process. A fleet of workers each keeps its own breaker and discovers
an outage independently - acceptable at this scale, and stated in ADR-004's spirit of
not pretending: the global version needs shared state (Redis) and is deliberately out of
scope for EAP-45.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from eap.llm.base import LLMError

T = TypeVar("T")


class CircuitOpenError(LLMError):
    """The breaker is open: the provider was down moments ago, so fail fast, retry later.

    Subclasses LLMError so every existing `except LLMError` path - the runner recording a
    failed run, the API's 503 on ingest - handles it with zero new code.
    """


async def call_with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_s: float,
    is_transient: Callable[[Exception], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """
    Run op, retrying transient failures with exponential backoff and FULL jitter.

    Full jitter (delay drawn uniformly from [0, base * 2^n]) over fixed steps because a
    provider blip hits every in-flight request at once - fixed delays would march them all
    back in lockstep and recreate the spike that tripped them.

    Non-transient errors re-raise immediately: retrying a bad API key or an invalid
    request is spam with extra latency, not resilience.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await op()
        except Exception as exc:  # noqa: BLE001 - classified right below
            if not is_transient(exc):
                raise
            last = exc
            if attempt < attempts - 1:
                await sleep(random.uniform(0, base_delay_s * (2**attempt)))
    assert last is not None
    raise last


class CircuitBreaker:
    """
    closed -> (N consecutive failures) -> open -> (cooldown) -> half-open probe
    -> success closes / failure re-opens.

    asyncio-concurrency-safe by construction: state changes happen only between awaits of
    the wrapped op, and a single event loop runs them - no locks needed, and none taken.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    async def call(self, op: Callable[[], Awaitable[T]]) -> T:
        if self._opened_at is not None:
            cooled_down = self._clock() - self._opened_at >= self._cooldown
            if not cooled_down or self._probe_in_flight:
                raise CircuitOpenError("provider unavailable; circuit open, failing fast")
            # Half-open: exactly one request through as the probe; everyone else keeps
            # failing fast until the probe reports back.
            self._probe_in_flight = True
        try:
            result = await op()
        except CircuitOpenError:
            raise
        except Exception:
            self._probe_in_flight = False
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._opened_at = self._clock()
            raise
        self._probe_in_flight = False
        self._consecutive_failures = 0
        self._opened_at = None
        return result

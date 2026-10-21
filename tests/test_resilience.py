"""
EAP-45: the supplier can drop a delivery, the warehouse can burn down, and one customer
can order fifty dinners - none of it takes the restaurant with it.

All unit tests: fake sleep (no real waiting), fake clock (no real cooldowns), and a
counter instead of a model. The breaker and retry are pure control flow, which is exactly
why they can be tested to the edge in milliseconds.
"""

import asyncio

import pytest

from eap.core.config import get_settings
from eap.llm.base import LLMError
from eap.llm.resilience import CircuitBreaker, CircuitOpenError, call_with_retry
from eap.workflows import activities


class Flaky:
    """Fails `failures` times with `exc`, then returns 'ok'. Counts every call."""

    def __init__(self, failures: int, exc: Exception) -> None:
        self.failures = failures
        self.exc = exc
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return "ok"


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _transient(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError)


async def test_a_transient_failure_is_absorbed() -> None:
    op, sleep = Flaky(2, TimeoutError()), FakeSleep()

    result = await call_with_retry(op, attempts=3, base_delay_s=0.2, is_transient=_transient, sleep=sleep)

    assert result == "ok"
    assert op.calls == 3
    # Full jitter: each delay is drawn from [0, base * 2^n] - bounded, not fixed, so a
    # provider blip does not march every caller back in lockstep.
    assert len(sleep.delays) == 2
    assert 0 <= sleep.delays[0] <= 0.2
    assert 0 <= sleep.delays[1] <= 0.4


async def test_a_permanent_failure_is_not_retried() -> None:
    op, sleep = Flaky(5, ValueError("bad api key")), FakeSleep()

    with pytest.raises(ValueError):
        await call_with_retry(op, attempts=3, base_delay_s=0.2, is_transient=_transient, sleep=sleep)

    assert op.calls == 1  # retrying a bad key is spam, not resilience
    assert sleep.delays == []


async def test_exhausted_attempts_raise_the_last_error() -> None:
    op, sleep = Flaky(99, TimeoutError()), FakeSleep()

    with pytest.raises(TimeoutError):
        await call_with_retry(op, attempts=3, base_delay_s=0.2, is_transient=_transient, sleep=sleep)

    assert op.calls == 3


async def test_the_breaker_opens_after_consecutive_failures() -> None:
    clock = [0.0]
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=lambda: clock[0])
    op = Flaky(99, TimeoutError())

    for _ in range(3):
        with pytest.raises(TimeoutError):
            await breaker.call(op)

    # Open: the next call never reaches the provider - microseconds, not a timeout.
    with pytest.raises(CircuitOpenError):
        await breaker.call(op)
    assert op.calls == 3
    # CircuitOpenError IS an LLMError, so every existing failure path handles it.
    assert issubclass(CircuitOpenError, LLMError)


async def test_a_successful_probe_closes_the_breaker() -> None:
    clock = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=lambda: clock[0])
    op = Flaky(2, TimeoutError())  # fails twice, then recovers

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await breaker.call(op)

    clock[0] = 31.0  # cooldown passed: one probe goes through
    assert await breaker.call(op) == "ok"
    assert not breaker.is_open
    assert await breaker.call(op) == "ok"  # closed for everyone again


async def test_a_failed_probe_reopens_for_a_fresh_cooldown() -> None:
    clock = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=lambda: clock[0])
    op = Flaky(99, TimeoutError())

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await breaker.call(op)

    clock[0] = 31.0
    with pytest.raises(TimeoutError):  # the probe itself reaches the provider and fails
        await breaker.call(op)
    with pytest.raises(CircuitOpenError):  # and the door slams again
        await breaker.call(op)
    clock[0] = 45.0  # a fresh cooldown counts from the FAILED probe, not the first opening
    with pytest.raises(CircuitOpenError):
        await breaker.call(op)


async def test_one_tenant_runs_at_most_its_cap_concurrently(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_MAX_CONCURRENT_RUNS", "2")
    get_settings.cache_clear()
    monkeypatch.setattr(activities, "_tenant_slots", {})
    in_flight, peak = [0], [0]

    async def one_run() -> None:
        async with activities._slot("tenant-a"):
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await asyncio.sleep(0.01)
            in_flight[0] -= 1

    await asyncio.gather(*(one_run() for _ in range(6)))

    assert peak[0] == 2  # six orders, two stoves, nobody lost - just queued


async def test_tenants_do_not_share_a_gate(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_MAX_CONCURRENT_RUNS", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(activities, "_tenant_slots", {})
    peak, in_flight = [0], [0]

    async def one_run(tenant: str) -> None:
        async with activities._slot(tenant):
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await asyncio.sleep(0.01)
            in_flight[0] -= 1

    # Two tenants, cap 1 EACH: both run at once, so the combined peak is 2 - tenant B is
    # never stuck behind tenant A's backlog.
    await asyncio.gather(one_run("tenant-a"), one_run("tenant-b"))

    assert peak[0] == 2

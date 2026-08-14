"""Circuit breaker unit tests with an injected clock (Phase 9)."""

from sentinel.circuit_breaker import (
    FAILURE_THRESHOLD,
    OPEN_TIMEOUT_SECONDS,
    BreakerState,
    CircuitBreaker,
)


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


def _breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(now=clock)


def test_starts_closed_with_zero_failures() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_count == 0
    assert breaker.is_open() is False


def test_opens_exactly_at_failure_threshold() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD - 1):
        breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.failure_count == FAILURE_THRESHOLD
    assert breaker.is_open() is True


def test_success_in_closed_resets_failure_count() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count == 0
    assert breaker.state is BreakerState.CLOSED


def test_open_remains_open_before_timeout() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    clock.advance(OPEN_TIMEOUT_SECONDS - 0.001)
    assert breaker.is_open() is True
    assert breaker.state is BreakerState.OPEN


def test_open_transitions_to_half_open_after_timeout() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    clock.advance(OPEN_TIMEOUT_SECONDS)
    assert breaker.is_open() is False
    assert breaker.state is BreakerState.HALF_OPEN


def test_half_open_success_closes_and_resets() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    clock.advance(OPEN_TIMEOUT_SECONDS)
    assert breaker.is_open() is False
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_count == 0
    assert breaker.is_open() is False


def test_half_open_failure_reopens() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    clock.advance(OPEN_TIMEOUT_SECONDS)
    assert breaker.is_open() is False
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.is_open() is True


def test_probe_failure_starts_fresh_quarantine() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    clock.advance(OPEN_TIMEOUT_SECONDS)
    assert breaker.is_open() is False
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock.advance(OPEN_TIMEOUT_SECONDS - 0.001)
    assert breaker.is_open() is True
    clock.advance(0.001)
    assert breaker.is_open() is False
    assert breaker.state is BreakerState.HALF_OPEN


def test_breakers_are_per_process_isolated() -> None:
    clock = FakeClock()
    first = _breaker(clock)
    second = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        first.record_failure()
    assert first.state is BreakerState.OPEN
    assert second.state is BreakerState.CLOSED
    assert second.failure_count == 0
    assert second.is_open() is False


def test_record_failure_while_open_is_noop() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.failure_count == FAILURE_THRESHOLD


def test_record_success_while_open_is_noop() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    breaker.record_success()
    assert breaker.state is BreakerState.OPEN

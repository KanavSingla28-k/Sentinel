"""Decision observability: structured deny logging and bounded metrics (Phases 12, 19).

Structured deny logs carry identity_mode (a bounded enum: tenant_jwt or
anonymous), identity_hash (the sha256 hash of the rate-limit identity — the
tenant id for tenant_jwt policies, the anonymous cookie/ip identity for
anonymous policies; never the raw value), endpoint_id, decision_reason,
evaluation latency, and breaker state. Prometheus metrics are keyed only by
endpoint_id and decision_reason — both bounded label sets: endpoint_id is an
explicit configured id (ADR-009) and decision_reason is the closed
DecisionReason enum. Collectors are process-wide by Prometheus semantics and
are registered once on the default registry; tests inject a private registry
through the constructor.
"""

import logging

from prometheus_client import CollectorRegistry, Counter, Histogram

from sentinel.circuit_breaker import BreakerState
from sentinel.models import Decision, IdentityMode

_LOGGER_NAME = "sentinel"
_LABELS = ("endpoint_id", "decision_reason")

_decisions = Counter(
    "sentinel_decisions_total",
    "Rate-limit decisions by endpoint and reason",
    labelnames=_LABELS,
)
_latency = Histogram(
    "sentinel_evaluate_latency_microseconds",
    "Rate-limit evaluation latency by endpoint and reason",
    labelnames=_LABELS,
)


class SentinelObservability:
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(_LOGGER_NAME)
        if registry is None:
            self._decisions = _decisions
            self._latency = _latency
        else:
            self._decisions = Counter(
                "sentinel_decisions_total",
                "Rate-limit decisions by endpoint and reason",
                labelnames=_LABELS,
                registry=registry,
            )
            self._latency = Histogram(
                "sentinel_evaluate_latency_microseconds",
                "Rate-limit evaluation latency by endpoint and reason",
                labelnames=_LABELS,
                registry=registry,
            )

    def record_decision(
        self,
        identity_mode: IdentityMode,
        identity_hash: str,
        endpoint_id: str,
        decision: Decision,
        latency_micro: int,
        breaker_state: BreakerState,
    ) -> None:
        self._decisions.labels(endpoint_id, decision.reason.value).inc()
        self._latency.labels(endpoint_id, decision.reason.value).observe(latency_micro)
        if not decision.allowed:
            self._logger.warning(
                "rate limit decision denied",
                extra={
                    "identity_mode": identity_mode.value,
                    "identity_hash": identity_hash,
                    "endpoint_id": endpoint_id,
                    "decision_reason": decision.reason.value,
                    "latency_micro": latency_micro,
                    "breaker_state": breaker_state.value,
                },
            )

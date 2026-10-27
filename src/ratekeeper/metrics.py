"""Prometheus metrics, served at GET /metrics.

Each app gets its own CollectorRegistry instead of the library's global one,
so tests can create many apps without "metric already registered" errors.

Labels are per policy, never per identity: every distinct label value is a
separate time series. There are tens of policies but potentially millions of
identities, which would overwhelm the metrics system.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Enum, Histogram

class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.checks = Counter(
            "ratekeeper_checks",
            "Checks by policy and outcome (allowed, rejected, degraded_allowed, degraded_rejected)",
            ["policy", "outcome"],
            registry=self.registry,
        )
        self.limiter_latency = Histogram(
            "ratekeeper_limiter_latency_seconds",
            "Time spent asking Redis for a decision",
            buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
            registry=self.registry,
        )
        self.circuit_state = Enum(
            "ratekeeper_redis_circuit",
            "State of the circuit breaker protecting Redis",
            states=["closed", "open", "half_open"],
            registry=self.registry,
        )

    def count_check(self, policy: str, outcome: str) -> None:
        self.checks.labels(policy=policy, outcome=outcome).inc()

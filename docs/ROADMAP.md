# Roadmap

## Done

- **Design.** Decisions and trade-offs in [DESIGN.md](DESIGN.md); algorithms in [ALGORITHMS.md](ALGORITHMS.md).
- **Correct core.** Four algorithms as atomic Lua scripts with integer math, multi-band all-or-nothing, variable cost. A Python reference model, property tests for its guarantees, and a Redis-vs-model equivalence test.
- **Service.** FastAPI API, versioned policies in Postgres with history, PUT + If-Match, cached policy lookup with pub/sub invalidation, Prometheus metrics, health endpoints.
- **Failure handling.** Timeouts, circuit breaker, per-policy fail-open/fail-closed, readiness that survives dependency outages.
- **CI.** Lint, type-check and every test (including real Redis/Postgres) on each push.
- **Python client.** `ratekeeper-client` (async, httpx) with a FastAPI dependency and middleware, plus a demo app. 


### Scale
- **Local quota leasing.** Each instance borrows a batch of units from Redis (one round trip per batch instead of per request) and hands them out from memory. Leased units are already deducted in Redis, so this can never over-admit. Unused units in a lease simply expire, so it may under-admit by at most `instances * batch` per lease period. Measure throughput against that accuracy cost.
- **Redis Cluster test.** Run the equivalence and concurrency tests against a 3-primary cluster to prove the hash-tag key design.

### Measure
- **Benchmarks with methodology.** Use a fixed-rate load generator (k6 or Locust) so latency spikes aren't hidden, and record p50/p99, hardware, versions, run count and warm-up. Compare algorithms on latency, Redis CPU and memory per identity.
- **Grafana dashboard** and example Prometheus alert rules.

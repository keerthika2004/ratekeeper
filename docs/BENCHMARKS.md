# Benchmarks

Numbers are only meaningful with the method next to them, so both are here.
Treat absolute values as specific to this machine; the comparisons between
algorithms and between request rates are what carry over.

## Setup

| | |
|---|---|
| Machine | _e.g. MacBook Air M1, 8 GB RAM_ |
| OS | _e.g. macOS 15.x_ |
| Docker | _Docker Desktop version, CPU/memory given to it_ |
| Redis / Postgres | redis:7.4-alpine / postgres:16-alpine (docker compose) |
| Python | 3.12 |
| App | one uvicorn process, `--log-level warning`, no `--reload` |
| Load generator | k6 _version_, on the same machine |
| Date / commit | _date, git commit hash_ |

Everything runs on one laptop, so the load generator, the app and Redis compete
for the same CPU. A real deployment would put them on separate machines.

## 1. Inside Redis: `benchmarks/algorithms.py`

Calls the limiter directly (no HTTP). "Redis CPU per call" comes from Redis' own
`INFO commandstats`, so it excludes the network.

## Memory per identity (bytes, band completely full)

| Algorithm | limit 10 | limit 100 | limit 1,000 | limit 10,000 |
|---|---:|---:|---:|---:|
| FIXED_WINDOW | 104 | 104 | 112 | 112 |
| SLIDING_WINDOW_LOG | 320 | 1,472 | 87,224 | 974,463 |
| SLIDING_WINDOW_COUNTER | 128 | 128 | 128 | 128 |
| GCRA | 72 | 72 | 72 | 72 |

## Cost per check (5,000 sequential calls per algorithm)

| Algorithm | Redis CPU per call (us) | Round trip p50 (us) | Round trip p99 (us) |
|---|---:|---:|---:|
| FIXED_WINDOW | 25.0 | 633 | 1331 |
| SLIDING_WINDOW_LOG | 39.1 | 623 | 1756 |
| SLIDING_WINDOW_COUNTER | 28.2 | 606 | 1682 |
| GCRA | 27.9 | 728 | 1347 |

**What to look for:** the sliding window log's memory grows with the limit (one
entry per admitted request); the other three stay flat. That's the trade-off
in [ALGORITHMS.md](ALGORITHMS.md), now measured.

## 2. Whole service over HTTP: `benchmarks/check.js` (k6)

`POST /v1/check` at a constant request rate for 60 s, spread over 10,000 users,
policy 100 requests/second per user. Each configuration was run _3_ times; the
table shows the median run.

### Algorithms at 1,000 requests/second

| Algorithm | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
|---|---:|---:|---:|---:|
| FIXED_WINDOW | | | | |
| SLIDING_WINDOW_LOG | | | | |
| SLIDING_WINDOW_COUNTER | | | | |
| GCRA | | | | |

### GCRA at increasing request rates

| Rate (req/s) | p50 (ms) | p99 (ms) | Errors | Dropped iterations |
|---:|---:|---:|---:|---:|
| 500 | | | | |
| 1,000 | | | | |
| 2,000 | | | | |
| 4,000 | | | | |

**Maximum sustainable rate:** the highest rate with p99 under _X_ ms, under 1%
errors and no dropped iterations. (Dropped iterations mean k6 itself couldn't
keep up, so the result at that rate doesn't count.)

## How to reproduce

```bash
docker compose up -d redis postgres
uv run alembic upgrade head
uv run python benchmarks/algorithms.py

uv run uvicorn ratekeeper.main:app --port 8000 --log-level warning   # separate terminal
k6 run -e ALGORITHM=GCRA -e RATE=1000 benchmarks/check.js
```
 
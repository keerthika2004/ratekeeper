# Design decisions

Each decision is written the same way: **what we did**, **why**, **what else we
could have done**, and **why we didn't**. The goal is that you can defend every
line of this project in an interview.

---

## The big picture

```
                    ┌──────────────── ratekeeper instance (any number of these) ────────────────┐
 gateway/service ─► │  POST /v1/check ─► CheckService ─► PolicyCache (memory) ◄─ reload ─ Postgres │
                    │                        │                  ▲                     (policies, │
                    │                        ▼                  └─ pub/sub ─┐          history)  │
                    │                  CircuitBreaker                       │                    │
                    │                        ▼                              │                    │
                    │                 RedisRateLimiter ── one Lua call ──► Redis (counters)      │
 admin ───────────► │  PUT /v1/policies/{name} ─► PolicyService ─► Postgres ─► publish change    │
                    └────────────────────────────────────────────────────────────────────────────┘
```

- **Hot path** (`/v1/check`): memory → one Redis round trip → answer. It never
  touches Postgres while the policy is cached.
- **Cold path** (`/v1/policies`): Postgres, transactional, audited. Rare.

Code layout follows the same split: `core/` has no framework imports at all;
`api/` is the only place that knows about HTTP.

---

## 1. Python 3.12 + FastAPI, fully async

**What:** FastAPI on uvicorn; Redis via `redis.asyncio`; Postgres via SQLAlchemy's
async engine on `asyncpg`.

**Why FastAPI:** it's the most in-demand Python API framework right now. Request
validation and OpenAPI docs (`/docs`) come free from type hints, and it's built
for async.

**Why async:** a rate limiter spends nearly all its time *waiting*: on Redis, on
Postgres. With async, one process handles thousands of waiting requests on a
single thread, switching to whichever request's data has arrived. There is no
CPU-heavy work that async would struggle with.

**Alternatives:**
- *Flask*: simple and familiar, but synchronous by default; each waiting request ties up a thread.
- *Django + DRF*: excellent for database-heavy apps with admin screens; heavier than needed for two tables and a hot path.
- *Sync FastAPI with threads*: works, but threads cost far more memory than async tasks under high concurrency.

**Scaling:** one uvicorn process per container; scale by running more containers
(see the two app instances in docker-compose). Each process has its own cache
and circuit breaker, which is fine because both are per-process by design.

---

## 2. uv for packaging

**What:** `pyproject.toml` (the standard) managed with **uv**; dependencies pinned in
`uv.lock`.

**Why:** uv is the fast, modern standard. One tool creates the virtual environment,
installs Python itself, resolves and locks dependencies, and runs commands
(`uv run pytest`). The lock file makes every install identical.

**Alternatives:** *pip + requirements.txt* (no real lock file; you hand-pin versions),
*Poetry* (popular, but slower and uses its own config format).

---

## 3. Where the counters live: Redis

**What:** all rate-limit state is in Redis, shared by every app instance.

**Why:** a limit is only meaningful if all instances share it. With 3 instances each
keeping its own count, "10 per minute" really means "30 per minute". Redis is in
memory (sub-millisecond), with built-in key expiry and server-side scripting.

**Alternatives:**
- *In-process memory*: fastest, but per-instance, so the limit multiplies with the instance count.
- *Postgres*: durable, but a row update per request is roughly 10-100x slower, and row locks on a popular identity become a bottleneck.
- *Memcached*: fast, but no scripting, so no way to read-decide-write atomically.

---

## 4. Atomicity: one Lua script per check

**What:** each check is one `EVALSHA` call. Redis runs the whole script (read state,
decide, write state) with nothing else in between.

**Why:** the classic bug is *check-then-act*:

```python
count = await r.get(key)  # two requests both read 9
if count < 10:  # both decide "allowed"
    await r.incr(key)  # count becomes 11
```

Under load many requests read the same old value and all get through. One
script removes the gap. `test_exactly_the_limit_is_admitted_under_contention`
fires 500 concurrent requests at a limit of 50 and requires exactly 50 to pass.

**Alternatives:**
- *MULTI/EXEC transactions*: batch commands, but the batch can't branch on a value it reads, and we need "if count < limit".
- *WATCH + retry*: correct, but under contention most attempts fail and retry, the opposite of what a limiter facing a flood needs.
- *A distributed lock*: two extra round trips per check, and a crashed lock holder stalls everyone.
- *Redis Functions (Redis 7+)*: the modern version of scripts; a fine choice. `EVALSHA` works on every Redis version and managed service.

redis-py's `register_script` sends `EVALSHA` (just the hash) and falls back to `EVAL`
(full source) automatically when Redis doesn't know the script yet.

---

## 5. Whole-number microseconds everywhere

**What:** time is an integer count of microseconds; every number in the scripts is an
integer; divisions are explicit `floor_div` / `ceil_div`.

**Why:** Lua stores numbers as 64-bit floats. They hold every whole number up to 2^53
(about 9 x 10^15) exactly, but fractions and huge numbers get rounded. Two traps:

1. `tostring(1786532313123456)` in Lua gives `"1.7865323131235e+15"`, losing the last
   digits. We format with `string.format('%d', n)` instead.
2. Fractional seconds like `0.1 + 0.2` are not exactly `0.3`, so tiny errors pile up
   and decisions can flip at the edges.

`core/rules.py` bounds every input so that every value and product the scripts
compute stays below 2^53; each bound says which calculation it protects.

**Why micro- rather than milliseconds:** GCRA's per-unit interval is `window / limit`. At
1,000,000 per second that's 1 µs; in milliseconds it would round to 0 or 1 ms.

**Payoff:** the math is exact, so the Python reference model computes the *same
numbers*, and tests can demand exact equality instead of "close enough".

---

## 6. Time comes from Redis, and tests can inject it

**What:** in production each script reads Redis' clock (`TIME`). Tests pass a time in.

**Why Redis' clock:** app servers' clocks drift apart. There's only one Redis clock per key.

**Why injectable:** tests can jump "2 seconds later" instantly and get the same result
every run. No `asyncio.sleep`, no flaky timing.

**Rejected: time sent by the client.** A client could lie and give itself a fresh window.

**Trade-off:** Redis TTLs follow Redis' real clock and can't follow a fake one, so in
simulated mode the scripts skip `PEXPIRE`. That's safe because expiry only frees
memory; no decision depends on it. `test_every_key_expires_on_its_own` checks TTLs
with the real clock.

---

## 7. Redis key layout

```
rk:{<policy>:<identity>}:v<version>:b<band>
```

| Part | Why |
|---|---|
| policy name | Two policies never share a counter (the original keyed by algorithm + identity only, so "login" and "search" for the same user shared one). |
| `v<version>` | Editing a policy starts fresh counters (see §11). |
| `{...}` hash tag | In Redis Cluster only the part in braces decides which node owns a key. All bands of one identity land on the same node, which a multi-key script requires. Works unchanged on a single Redis. |
| percent-encoded identity | An identity like `a}:{b` can't break out of the hash tag or collide with other keys. |

Every key a script touches is passed in `KEYS` (none built inside the script),
which Redis Cluster also requires. That's why fixed window stores
`{window number, count}` in one fixed key instead of one key per window.

---

## 8. Four algorithms, one calling convention

Fixed window, sliding log, sliding counter, GCRA; see [ALGORITHMS.md](ALGORITHMS.md).
All share one argument layout and one result shape (`lua/prelude.lua`), so a fifth
algorithm means one Lua file plus one reference-model method.

No separate token bucket: GCRA admits exactly the same requests while storing one
number instead of two.

---

## 9. Bands and cost

**Bands:** "10 per second AND 1,000 per hour" stops both bursts and sustained abuse.
Bands are checked in one script, all-or-nothing.

*Rejected:* two separate policies checked one after the other. If the first charges and
the second rejects, quota has been spent on a request that never happened.

**Cost:** a request can consume N units (a bulk export = 10). A cost larger than any band
can ever hold returns `422` instead of being rejected forever.

---

## 10. Callers name a policy; they can't send limits

`/v1/check` takes `{policy, identity, cost}` only. Whoever owns the policy should set
the limit, not whoever is being limited. It also keeps metric labels bounded and
makes changing a limit one admin action instead of a redeploy of every caller.

---

## 11. Policies in Postgres: versioned, audited, migrated with Alembic

**What:** a `policies` table plus an append-only `policy_history`, written in the same
transaction. SQL via **SQLAlchemy Core** (`text()` queries, no ORM). Schema changes are
**Alembic** migrations.

**Why Postgres:** policies are configuration that must survive restarts; Redis here
runs without persistence on purpose.

**Why versions:** the version is part of every Redis key. Changing a policy (say from
fixed window to GCRA) makes every identity start fresh, instead of the new algorithm
misreading state the old one wrote. A deleted and re-created policy continues
numbering from its history, so it can never pick up old counters.

**Why SQLAlchemy Core instead of the ORM:** two small tables. ORM models and sessions
would add more concepts than code, and would hide the Postgres features we rely on
(advisory locks, upsert, jsonb). Core still gives us connection pooling,
transactions and Alembic.

**Alternatives:** *the SQLAlchemy ORM / SQLModel* (great for many related tables;
overkill here), *raw asyncpg* (fastest, but no migration tool to pair with).

**Why migrations run as their own step:** the `migrate` service in docker-compose runs
`alembic upgrade head` once, and the apps start only after it succeeds. If every
app instance migrated at startup, several could race; the original created its
tables at startup from every instance. `migrations/env.py` also takes a Postgres
advisory lock, so even concurrent upgrade runs take turns.

---

## 12. Safe retries without an Idempotency-Key

**What:** `PUT /v1/policies/{name}` sends the complete desired policy. If it equals
what's stored, nothing happens: no new version, no history row.

**Why:** that makes retries naturally safe. A client that timed out can send the same
PUT again.

**Optimistic locking (ETag / If-Match):** responses carry `ETag: "<version>"`. Sending
`If-Match: "3"` means "only apply this if the policy is still at version 3". If
another admin changed it first, you get `412` instead of silently overwriting them.

**Alternative:** `POST` + `Idempotency-Key` with stored responses (what the original
did). That's needed for operations that aren't naturally idempotent. PUT makes it
unnecessary: no key store, no key/body mismatch case, no gap between "write
policy" and "remember key".

**Concurrent writers:** `pg_advisory_xact_lock(hashtext(name))` makes writers of the same
name take turns until their transaction ends. *Why not `SELECT ... FOR UPDATE`?* It
can't lock a row that doesn't exist yet, so two concurrent *creates* would race.

---

## 13. Policy cache: memory, pub/sub, background refresh

**What** (`policies/cache.py`, about 100 lines, fully unit-tested):
1. **Pub/sub invalidation.** After a change commits, the instance publishes the name on a Redis channel and every instance drops its copy.
2. **Background refresh after 30 s.** Pub/sub is fire-and-forget, so a message can be missed; the refresh bounds staleness. The old copy is served while the refresh runs.
3. **Serve stale for up to 1 h if Postgres is down.** A failed refresh keeps the old copy.
4. **Single flight.** Ten concurrent requests for an uncached policy share one database query.
5. **Race-safe.** A load that was in flight when an invalidation arrived may have read the old row, so its result isn't cached.

**Why:** the original queried Postgres on every check, adding a database round trip to
the hot path and making rate limiting fail whenever Postgres did.

**Why write it instead of using a library:** `cachetools` and similar libraries are
synchronous and have none of refresh-while-serving, serve-stale-on-error or
single-flight. `aiocache` focuses on decorators over remote backends. The
behaviour here *is* the design, so it's written out and tested directly.

**Pub/sub needs its own connection:** the check path's Redis connection times out
reads after 100 ms. A subscription sits idle between messages, so it uses a
second connection without that timeout. Otherwise it would disconnect constantly.

---

## 14. When Redis is down: timeout, circuit breaker, failure mode

**What:**
1. `asyncio.timeout(0.1)` around each check (plus socket timeouts) bounds any single Redis call.
2. A **circuit breaker** (`check/breaker.py`) watches the last 20 calls. If at least half
   fail, it *opens* for 5 s: calls fail instantly without touching Redis. Then a few
   trial calls test whether Redis is back.
3. The policy's **failure mode** decides the answer: `OPEN` allows, `CLOSED` rejects. The
   response says `"degraded": true` and has the header `X-RateLimit-Degraded: true`.

**Why the breaker, if there's already a timeout?** Without it, every request during an
outage waits the full 100 ms before falling back. At 5,000 requests/second, that's
500 requests stuck at any moment. With it, fallbacks take microseconds.

**Why per-policy failure mode:** a limit on reading product pages should fail **open**
(don't take the site down because the limiter is down). A limit on OTP SMS or login
attempts should fail **closed** (an outage must not become a free-for-all).

**Only real outages count.** Connection errors and timeouts trip the breaker. A Lua
`ResponseError` is a bug in *our* code, so it surfaces as a 500 instead of quietly
putting every policy into its failure mode (`test_script_errors_are_bugs_not_outages`).

**Why our own breaker:** `pybreaker` is built around threads, and the async ones
are thinly maintained. The state machine is about 80 lines and fully tested.

**Health:** `/health/ready` deliberately ignores Redis and Postgres. The service still
answers while they're down, and marking every instance "not ready" at once would
turn a degraded service into no service. `/health` shows each dependency's real state.

---

## 15. `/v1/check` returns 200 with the decision in the body

The caller is a gateway or service, not the end user. The HTTP status describes the
*call to ratekeeper*; a 429 would blur "the user is over their limit" with "you, the
gateway, are calling too fast". Envoy's rate-limit protocol works the same way. The
gateway turns `allowed: false` into a 429 for its own client, copying our
`X-RateLimit-*` and `Retry-After` headers.

---

## 16. Errors and validation

- Errors use **RFC 9457 problem details** (`application/problem+json`), the same shape everywhere.
- **422** when the JSON doesn't match the schema (FastAPI's convention); **400** when it's well-formed but breaks a policy rule; **412** on version conflict; **404**, **503** as usual.
- Request models use `extra="forbid"`, so a typo like `"limt"` is an error instead of being silently ignored.
- **Pydantic only at the edge.** Core types are frozen dataclasses: no framework dependency, immutable, and equal when their fields are equal (which the "repeated PUT is a no-op" rule relies on).

---

## 17. Observability: metrics instead of a custom alerter

`GET /metrics` (prometheus_client):
- `ratekeeper_checks_total{policy, outcome}`, where outcome is `allowed`, `rejected`, `degraded_allowed` or `degraded_rejected`
- `ratekeeper_limiter_latency_seconds` (histogram, for p50/p99)
- `ratekeeper_redis_circuit` (closed / open / half_open)

"Alert when a key is rejected a lot" is one Prometheus alert rule. The original built
a custom Redis Streams consumer for it, which was more code to run and had a
crash-recovery bug.

**Labels are per policy, never per identity:** every label value is a separate time
series, and there are potentially millions of identities.

---

## 18. Testing strategy: prove it, don't just try it

| Test | What it proves |
|---|---|
| `test_reference_examples.py` | Each algorithm's behaviour, on readable concrete numbers |
| `test_reference_properties.py` | Hypothesis, thousands of random scenarios: never over-admits; `retry_after` and `reset_after` exact to the µs; rejections change nothing |
| `integration/test_redis_equivalence.py` | Real Redis returns *identical* decisions to the model for random scenarios, so the model's guarantees hold in production |
| `integration/test_redis_behaviour.py` | 500 concurrent requests at limit 50 → exactly 50; every key expires |
| `test_check_service.py`, `test_breaker.py`, `test_policy_cache.py` | Failure modes, breaker state machine, cache freshness and races |
| `integration/test_api.py` | HTTP API end to end: versions, If-Match, history, cache invalidation, errors, metrics, health |

**Why a reference model:** Lua inside Redis is hard to test directly. Plain Python is
easy to read and check with property tests. Proving the model right, then proving
Lua equals the model, splits one hard problem into two easy ones.

**Why property-based tests (Hypothesis):** hand-written examples only cover cases
you thought of. Random sequences find the ones you didn't, and Hypothesis
*shrinks* a failure to the smallest sequence that still fails.

**Why real Redis and Postgres (Testcontainers) instead of fakes:** fakes (fakeredis,
SQLite) behave differently in exactly the areas we care about: Lua semantics,
advisory locks, jsonb, upsert.

**Integration tests fail, never skip, without Docker.** Run `pytest -m "not integration"`
to choose to skip them; a missing Docker daemon can never quietly produce a green build.

---

## 19. The Python client (`clients/python`)

**Separate package, same repo.** An app that only *calls* ratekeeper shouldn't
install SQLAlchemy, asyncpg and Alembic. `ratekeeper-client` depends on `httpx` only
(plus FastAPI for the integration). A **uv workspace** keeps both packages in one
repo with one lock file.

**Two failure kinds, two behaviours:**
- *ratekeeper unreachable* (down, timeout, 5xx): an infrastructure problem, so a
 setting decides: allow (default), deny, or raise. It's the same idea as the
 server's per-policy failure mode, one level further out.
- *ratekeeper answers 4xx* (unknown policy, bad input): a bug in the calling app,
 so it always raises. Silently allowing would hide a misspelled policy name forever.

**250 ms timeout:** ratekeeper's own Redis timeout is 100 ms; the rest is network
headroom. A sick limiter must not make every request in the calling app slow.

**Dependency *and* middleware:**
- The **dependency** (`rate_limit(...)`) is per route: each route picks its policy,
 cost, and who is being limited (user id, API key...). That's usually what you want.
- The **middleware** applies one policy to the whole app, skipping health and docs paths.

**Identity is the caller's choice.** The default is the client IP, with a clear
warning: behind a load balancer every user would share the balancer's IP, and
`X-Forwarded-For` can be faked unless a trusted proxy sets it.

**Headers on success too.** Allowed responses carry `X-RateLimit-*`, so
well-behaved clients can slow down before they're rejected.

**Tested twice:** against a fake ratekeeper (`httpx.MockTransport`) for every
branch, and end to end (`test_client_e2e.py`): demo app → client → real ratekeeper
→ real Redis and Postgres.

---


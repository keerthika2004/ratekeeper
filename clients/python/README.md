# ratekeeper-client

Async Python client for the [ratekeeper](../../README.md) rate limiter, with FastAPI integration.
Depends only on `httpx` (plus `fastapi` if you use the integration).

## Client

```python
from ratekeeper_client import RateKeeperClient

limiter = RateKeeperClient("http://ratekeeper:8000")

decision = await limiter.check("login", identity="user-42")      # cost defaults to 1
if not decision.allowed:
   ...  # reply 429; decision.headers has Retry-After and X-RateLimit-*
```

Create the client once and reuse it (it keeps connections open); close it with
`await limiter.aclose()` at shutdown.

| Option | Default | Meaning |
|---|---|---|
| `timeout` | `0.25` s | How long to wait for ratekeeper |
| `on_error` | `"allow"` | If ratekeeper can't be reached: `"allow"`, `"deny"` or `"raise"` |
| `http_client` | new `httpx.AsyncClient` | Bring your own (custom limits, test transport) |

A 4xx answer from ratekeeper (unknown policy, invalid input) always raises
`RateKeeperError`. That's a configuration bug, and quietly allowing would hide it.

## FastAPI

**Per route** (each route picks its policy and who is being limited):

```python
from fastapi import Depends
from ratekeeper_client.fastapi import rate_limit

def current_user(request) -> str:
   return request.state.user_id          # whatever your auth sets

@app.post("/login", dependencies=[Depends(rate_limit(limiter, "login"))])          # by client IP
async def login(): ...

@app.post("/export", dependencies=[Depends(rate_limit(limiter, "exports", key=current_user, cost=10))])
async def export(): ...
```

**Whole app** (one policy for every request; `/health`, `/docs` and `/openapi.json` are skipped):

```python
from ratekeeper_client.fastapi import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware, client=limiter, policy="api-default")
```

Rejected requests get `429 Too Many Requests` with `Retry-After` and `X-RateLimit-*`;
allowed ones get `X-RateLimit-*` too, so clients can slow down before they hit the limit.

**Identity behind a proxy:** the default key is the client IP. Behind a load balancer
that's the balancer's IP, so everyone would share one limit. Use the logged-in user,
or read `X-Forwarded-For` only if you trust the proxy that sets it.

A runnable example: [examples/demo_app.py](../../examples/demo_app.py).
 
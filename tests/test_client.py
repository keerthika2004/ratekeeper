"""The client and its FastAPI integration, against a fake ratekeeper (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from ratekeeper_client import OnError, RateKeeperClient, RateKeeperError, RateKeeperUnavailable
from ratekeeper_client.fastapi import RateLimitMiddleware, rate_limit


def fake_ratekeeper(limit: int) -> httpx.MockTransport:
   """Answers /v1/check like ratekeeper would: `limit` units per (policy, identity)."""
   used: dict[tuple[str, str], int] = {}

   def handle(request: httpx.Request) -> httpx.Response:
       body = json.loads(request.content)
       key = (body["policy"], body["identity"])
       allowed = used.get(key, 0) + body["cost"] <= limit
       if allowed:
           used[key] = used.get(key, 0) + body["cost"]
       remaining = limit - used.get(key, 0)
       headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset": "60"}
       if not allowed:
           headers["Retry-After"] = "60"
       return httpx.Response(
           200,
           headers=headers,
           json={
               "allowed": allowed,
               "degraded": False,
               "policy": body["policy"],
               "identity": body["identity"],
               "limit": limit,
               "remaining": remaining,
               "reset_after_ms": 60_000,
               "retry_after_ms": 0 if allowed else 60_000,
           },
       )

   return httpx.MockTransport(handle)


def answering(status: int, body: dict[str, object] | None = None) -> httpx.MockTransport:
   return httpx.MockTransport(lambda request: httpx.Response(status, json=body or {}))


def unreachable() -> httpx.MockTransport:
   def handle(request: httpx.Request) -> httpx.Response:
       raise httpx.ConnectError("connection refused", request=request)

   return httpx.MockTransport(handle)


def client_with(transport: httpx.MockTransport, on_error: OnError = OnError.ALLOW) -> RateKeeperClient:
   return RateKeeperClient("http://ratekeeper", on_error=on_error, http_client=httpx.AsyncClient(transport=transport))


# --- the client ------------------------------------------------------------


async def test_decisions_and_headers_come_through() -> None:
   limiter = client_with(fake_ratekeeper(limit=2))

   first = await limiter.check("login", "alice")
   assert first.allowed and first.remaining == 1
   assert first.headers["X-RateLimit-Remaining"] == "1"

   await limiter.check("login", "alice")
   rejected = await limiter.check("login", "alice")
   assert not rejected.allowed
   assert rejected.headers["Retry-After"] == "60"


@pytest.mark.parametrize(("on_error", "allowed"), [(OnError.ALLOW, True), (OnError.DENY, False)])
@pytest.mark.parametrize("transport", [unreachable(), answering(503)])
async def test_an_unreachable_service_falls_back_to_on_error(
   transport: httpx.MockTransport, on_error: OnError, allowed: bool
) -> None:
   decision = await client_with(transport, on_error).check("login", "alice")

   assert decision.allowed is allowed
   assert decision.degraded
   assert decision.headers["X-RateLimit-Degraded"] == "true"


async def test_on_error_raise_lets_the_caller_decide() -> None:
   with pytest.raises(RateKeeperUnavailable):
       await client_with(unreachable(), OnError.RAISE).check("login", "alice")


async def test_configuration_mistakes_are_loud_not_silently_allowed() -> None:
   limiter = client_with(answering(404, {"detail": "policy 'logn' not found"}))

   with pytest.raises(RateKeeperError, match="logn"):
       await limiter.check("logn", "alice")


# --- FastAPI integration ---------------------------------------------------


def demo_app(limiter: RateKeeperClient) -> FastAPI:
   app = FastAPI()

   def user_header(request: Request) -> str:
       return request.headers.get("X-User", "anonymous")

   @app.post("/login", dependencies=[Depends(rate_limit(limiter, "login", key=user_header))])
   async def login() -> dict[str, str]:
       return {"status": "logged in"}

   return app


async def call(app: FastAPI, method: str, path: str, user: str = "alice") -> httpx.Response:
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as http:
       return await http.request(method, path, headers={"X-User": user})


async def test_the_dependency_returns_429_with_headers_once_the_limit_is_reached() -> None:
   app = demo_app(client_with(fake_ratekeeper(limit=2)))

   for remaining in ("1", "0"):
       allowed = await call(app, "POST", "/login")
       assert allowed.status_code == 200
       assert allowed.headers["X-RateLimit-Remaining"] == remaining

   rejected = await call(app, "POST", "/login")
   assert rejected.status_code == 429
   assert rejected.headers["Retry-After"] == "60"

   # The key function decides who is limited: another user is unaffected.
   assert (await call(app, "POST", "/login", user="bob")).status_code == 200


async def test_the_middleware_limits_every_route_except_excluded_paths() -> None:
   app = FastAPI()
   app.add_middleware(RateLimitMiddleware, client=client_with(fake_ratekeeper(limit=1)), policy="api")

   @app.get("/items")
   async def items() -> list[str]:
       return []

   @app.get("/health")
   async def health() -> dict[str, str]:
       return {"status": "ok"}

   assert (await call(app, "GET", "/items")).status_code == 200
   assert (await call(app, "GET", "/items")).status_code == 429
   for _ in range(3):
       assert (await call(app, "GET", "/health")).status_code == 200
 
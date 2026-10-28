"""FastAPI integration. Two ways to protect an app:

rate_limit(...) is a dependency for individual routes. It's the flexible one:
each route picks its own policy, and the identity can come from anything the
request carries (logged-in user, API key, path parameter):

   limiter = RateKeeperClient("http://ratekeeper:8000")

   @app.post("/login", dependencies=[Depends(rate_limit(limiter, "login"))])
   async def login(): ...

RateLimitMiddleware applies one policy to every request (except excluded paths):

   app.add_middleware(RateLimitMiddleware, client=limiter, policy="api-default")

Either way, a rejected request gets 429 Too Many Requests with Retry-After and
X-RateLimit-* headers; an allowed one gets the X-RateLimit-* headers too, so
well-behaved clients can slow down before they hit the limit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from .client import Decision, RateKeeperClient

KeyFunc = Callable[[Request], str | Awaitable[str]]
"""Turns a request into the identity being limited. May be sync or async."""


def client_ip(request: Request) -> str:
   """Default identity: the caller's IP address.

   Behind a load balancer or proxy this is the proxy's address, so every user
   would share one limit. In that case pass a key function that reads the
   real client IP from X-Forwarded-For (only if you trust the proxy, since
   callers can fake that header), or better, the authenticated user's id.
   """
   return request.client.host if request.client else "unknown"


async def _identity(key: KeyFunc, request: Request) -> str:
   value = key(request)
   if isinstance(value, str):
       return value
   return await value


def rate_limit(
   client: RateKeeperClient, policy: str, *, key: KeyFunc = client_ip, cost: int = 1
) -> Callable[[Request, Response], Awaitable[Decision]]:
   """A FastAPI dependency that consumes `cost` units of `policy` per request.

   Use it in `dependencies=[Depends(...)]`, or as a parameter
   (`decision: Decision = Depends(...)`) if the route wants the numbers.
   """

   async def dependency(request: Request, response: Response) -> Decision:
       decision = await client.check(policy, await _identity(key, request), cost)
       if not decision.allowed:
           raise HTTPException(status_code=429, detail="Too many requests", headers=decision.headers)
       response.headers.update(decision.headers)
       return decision

   return dependency


class RateLimitMiddleware(BaseHTTPMiddleware):
   """One policy for every request, except paths starting with `exclude_paths`
   (health checks and docs shouldn't be counted or blocked)."""

   def __init__(
       self,
       app: ASGIApp,
       *,
       client: RateKeeperClient,
       policy: str,
       key: KeyFunc = client_ip,
       cost: int = 1,
       exclude_paths: tuple[str, ...] = ("/health", "/docs", "/openapi.json"),
   ) -> None:
       super().__init__(app)
       self._client = client
       self._policy = policy
       self._key = key
       self._cost = cost
       self._exclude_paths = exclude_paths

   async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
       if request.url.path.startswith(self._exclude_paths):
           return await call_next(request)
       decision = await self._client.check(self._policy, await _identity(self._key, request), self._cost)
       if not decision.allowed:
           return JSONResponse({"detail": "Too many requests"}, status_code=429, headers=decision.headers)
       response = await call_next(request)
       response.headers.update(decision.headers)
       return response
 
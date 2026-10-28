"""The client: one call per request your app wants to rate-limit.

   limiter = RateKeeperClient("http://ratekeeper:8000")
   decision = await limiter.check("login", identity=user_id)
   if not decision.allowed:
       ...  # reply 429 with decision.headers

Two kinds of failure are handled differently:

- The ratekeeper service can't be reached (down, timed out, 5xx). That's an
 infrastructure problem, so `on_error` decides: allow the request (default),
 deny it, or raise. Same idea as the server's per-policy failure mode, one
 level further out.
- ratekeeper answers 4xx (unknown policy, invalid identity, cost too high).
 That's a bug in the calling app's configuration, so it always raises
 RateKeeperError. Quietly allowing would hide the mistake forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Self

import httpx

# Headers ratekeeper sets that an app should pass on to its own client.
_FORWARDED_HEADERS = (
   "X-RateLimit-Limit",
   "X-RateLimit-Remaining",
   "X-RateLimit-Reset",
   "X-RateLimit-Degraded",
   "Retry-After",
)


class OnError(StrEnum):
   """What check() returns when the ratekeeper service itself can't be reached."""

   ALLOW = "allow"
   """Let the request through (fail open). Rate limiting is protection, not the product."""
   DENY = "deny"
   """Reject the request (fail closed). For endpoints that must never be unprotected."""
   RAISE = "raise"
   """Raise RateKeeperUnavailable and let the caller decide."""


class RateKeeperError(Exception):
   """ratekeeper refused the call itself: unknown policy, invalid input, ..."""


class RateKeeperUnavailable(RateKeeperError):
   """The ratekeeper service couldn't be reached or failed (only raised with OnError.RAISE)."""


@dataclass(frozen=True, slots=True)
class Decision:
   allowed: bool
   degraded: bool
   """True when the answer came from a failure mode, not from an actual count:
   either ratekeeper couldn't reach Redis, or this client couldn't reach ratekeeper."""
   limit: int | None
   remaining: int | None
   reset_after_ms: int | None
   retry_after_ms: int | None
   headers: dict[str, str] = field(default_factory=dict)
   """X-RateLimit-* and Retry-After, ready to copy onto your own response."""


class RateKeeperClient:
   def __init__(
       self,
       base_url: str,
       *,
       timeout: float = 0.25,
       on_error: OnError | str = OnError.ALLOW,
       http_client: httpx.AsyncClient | None = None,
   ) -> None:
       """
       timeout: seconds to wait for ratekeeper. Its own Redis timeout is 100 ms,
           so 250 ms leaves room for the network without letting a sick
           limiter make every request in your app slow.
       http_client: bring your own httpx.AsyncClient (connection limits, a test
           transport, ...). If you pass one, you also close it yourself.
       """
       self._url = base_url.rstrip("/") + "/v1/check"
       self._on_error = OnError(on_error)
       self._owns_http = http_client is None
       # One shared AsyncClient reuses connections (keep-alive) across checks;
       # opening a new connection per check would cost more than the check itself.
       self._http = http_client or httpx.AsyncClient(timeout=timeout)

   async def check(self, policy: str, identity: str, cost: int = 1) -> Decision:
       try:
           response = await self._http.post(self._url, json={"policy": policy, "identity": identity, "cost": cost})
       except httpx.HTTPError as error:  # connection refused, timeout, ...
           return self._unavailable(f"cannot reach ratekeeper: {error!r}", error)

       if response.status_code >= 500:
           return self._unavailable(f"ratekeeper failed with {response.status_code}", None)
       if response.status_code != 200:
           raise RateKeeperError(f"ratekeeper rejected the check ({response.status_code}): {_detail(response)}")

       body = response.json()
       return Decision(
           allowed=body["allowed"],
           degraded=body["degraded"],
           limit=body["limit"],
           remaining=body["remaining"],
           reset_after_ms=body["reset_after_ms"],
           retry_after_ms=body["retry_after_ms"],
           headers={name: response.headers[name] for name in _FORWARDED_HEADERS if name in response.headers},
       )

   def _unavailable(self, message: str, cause: Exception | None) -> Decision:
       if self._on_error is OnError.RAISE:
           raise RateKeeperUnavailable(message) from cause
       allowed = self._on_error is OnError.ALLOW
       headers = {"X-RateLimit-Degraded": "true"}
       if not allowed:
           headers["Retry-After"] = "1"
       return Decision(allowed, True, None, None, None, None, headers)

   async def aclose(self) -> None:
       if self._owns_http:
           await self._http.aclose()

   async def __aenter__(self) -> Self:
       return self

   async def __aexit__(
       self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
   ) -> None:
       await self.aclose()


def _detail(response: httpx.Response) -> str:
   try:
       return str(response.json().get("detail", response.text))
   except ValueError:
       return response.text
 
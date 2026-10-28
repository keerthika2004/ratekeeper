"""A small FastAPI app protected by ratekeeper, to try the client by hand.

1. Start ratekeeper (see the main README) on port 8000, and create the policies:

   curl -X PUT localhost:8000/v1/policies/login -H 'Content-Type: application/json' \
     -d '{"algorithm": "GCRA", "failure_mode": "CLOSED", "bands": [{"limit": 3, "window": "PT1M"}]}'
   curl -X PUT localhost:8000/v1/policies/search -H 'Content-Type: application/json' \
     -d '{"algorithm": "SLIDING_WINDOW_COUNTER", "bands": [{"limit": 10, "window": "PT10S"}]}'

2. Start this app on port 9000:

   uv run uvicorn examples.demo_app:app --port 9000

3. Call it a few times and watch the 429s and headers:

   for i in 1 2 3 4 5; do curl -si -X POST localhost:9000/login -H 'X-User: alice' | head -1; done
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from ratekeeper_client import Decision, RateKeeperClient
from ratekeeper_client.fastapi import client_ip, rate_limit

limiter = RateKeeperClient("http://localhost:8000")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
   yield
   await limiter.aclose()


app = FastAPI(title="ratekeeper demo", lifespan=lifespan)


def user_or_ip(request: Request) -> str:
   """Limit logged-in users by their id, everyone else by IP. (A real app would
   read the user from its auth token, not from a header the caller controls.)"""
   return request.headers.get("X-User") or client_ip(request)


@app.post("/login", dependencies=[Depends(rate_limit(limiter, "login", key=user_or_ip))])
async def login() -> dict[str, str]:
   return {"status": "logged in"}


@app.get("/search")
async def search(
   q: str, decision: Annotated[Decision, Depends(rate_limit(limiter, "search", key=user_or_ip))]
) -> dict[str, object]:
   # Taking the Decision as a parameter lets the route use the numbers too.
   return {"query": q, "results": [], "searches_left": decision.remaining}
 
"""End to end: a FastAPI app protected by the client, talking to the real
ratekeeper app (in-process), which talks to real Redis and Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from ratekeeper_client import RateKeeperClient
from ratekeeper_client.fastapi import rate_limit

from ratekeeper.config import Settings
from ratekeeper.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def ratekeeper_http(redis_url: str, database_url: str) -> AsyncIterator[httpx.AsyncClient]:
   service = create_app(Settings(redis_url=redis_url, database_url=database_url))
   async with service.router.lifespan_context(service):
       transport = httpx.ASGITransport(app=service)
       async with httpx.AsyncClient(transport=transport, base_url="http://ratekeeper") as http:
           yield http


async def test_a_protected_route_gets_429_from_the_real_service(ratekeeper_http: httpx.AsyncClient) -> None:
   created = await ratekeeper_http.put(
       "/v1/policies/e2e-login",
       json={"algorithm": "SLIDING_WINDOW_LOG", "bands": [{"limit": 3, "window": "PT1M"}]},
   )
   assert created.status_code == 201

   limiter = RateKeeperClient("http://ratekeeper", http_client=ratekeeper_http)
   app = FastAPI()

   def user(request: Request) -> str:
       return request.headers["X-User"]

   @app.post("/login", dependencies=[Depends(rate_limit(limiter, "e2e-login", key=user))])
   async def login() -> dict[str, str]:
       return {"status": "logged in"}

   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as http:
       statuses = [(await http.post("/login", headers={"X-User": "mallory"})).status_code for _ in range(4)]
       assert statuses == [200, 200, 200, 429]

       rejected = await http.post("/login", headers={"X-User": "mallory"})
       assert int(rejected.headers["Retry-After"]) >= 1
       assert rejected.headers["X-RateLimit-Limit"] == "3"
 
"""The whole application over HTTP, with real Postgres and Redis.

Each test uses its own policy names, so tests don't need to clean up after
each other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ratekeeper.config import Settings
from ratekeeper.main import create_app

pytestmark = pytest.mark.integration

@pytest.fixture
async def api(redis_url: str, database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(redis_url=redis_url, database_url=database_url))
    # Run startup/shutdown exactly as uvicorn would.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

def fixed_window(limit: int) -> dict[str, Any]:
    return {"algorithm": "FIXED_WINDOW", "bands": [{"limit": limit, "window": "PT1H"}]}

async def put(api: httpx.AsyncClient, name: str, body: dict[str, Any], if_match: str | None = None) -> httpx.Response:
    headers = {"If-Match": if_match} if if_match else {}
    return await api.put(f"/v1/policies/{name}", json=body, headers=headers)

async def check(api: httpx.AsyncClient, policy: str, identity: str, cost: int = 1) -> httpx.Response:
    return await api.post("/v1/check", json={"policy": policy, "identity": identity, "cost": cost})

async def test_policy_lifecycle_is_versioned_and_audited(api: httpx.AsyncClient) -> None:
    created = await put(api, "lifecycle", fixed_window(10))
    assert created.status_code == 201
    assert created.headers["ETag"] == '"1"'
    assert created.json()["failure_mode"] == "OPEN"
    assert created.json()["bands"][0]["window"] == "PT1H"

    # Same content again: nothing changes, so retries are harmless.
    repeated = await put(api, "lifecycle", fixed_window(10))
    assert repeated.status_code == 200
    assert repeated.headers["ETag"] == '"1"'

    updated = await put(api, "lifecycle", fixed_window(20), if_match='"1"')
    assert updated.headers["ETag"] == '"2"'

    # A stale If-Match means someone else changed it first.
    assert (await put(api, "lifecycle", fixed_window(30), if_match='"1"')).status_code == 412

    assert (await api.delete("/v1/policies/lifecycle", headers={"If-Match": '"2"'})).status_code == 204
    assert (await api.get("/v1/policies/lifecycle")).status_code == 404

    history = (await api.get("/v1/policies/lifecycle/history")).json()
    assert [entry["action"] for entry in history] == ["DELETED", "UPDATED", "CREATED"]

    # Re-creating continues the numbering, so old Redis counters are never reused.
    recreated = await put(api, "lifecycle", fixed_window(10))
    assert recreated.status_code == 201
    assert recreated.json()["version"] == 3

async def test_checks_consume_quota_then_report_rejection(api: httpx.AsyncClient) -> None:
    await put(api, "three-per-hour", fixed_window(3))

    for remaining in (2, 1, 0):
        response = await check(api, "three-per-hour", "alice")
        assert response.status_code == 200
        assert response.json()["allowed"] is True
        assert response.json()["remaining"] == remaining
        assert response.headers["X-RateLimit-Limit"] == "3"
        assert response.headers["X-RateLimit-Remaining"] == str(remaining)

    rejected = await check(api, "three-per-hour", "alice")
    assert rejected.status_code == 200
    assert rejected.json()["allowed"] is False
    assert rejected.json()["degraded"] is False
    assert rejected.json()["retry_after_ms"] > 0
    assert int(rejected.headers["Retry-After"]) >= 1

    # Another identity has its own quota.
    assert (await check(api, "three-per-hour", "bob")).json()["allowed"] is True

async def test_policy_changes_apply_to_the_very_next_check(api: httpx.AsyncClient) -> None:
    await put(api, "changing", fixed_window(1))
    assert (await check(api, "changing", "carol")).json()["allowed"] is True
    assert (await check(api, "changing", "carol")).json()["allowed"] is False

    # New version: the cache is invalidated after commit and counters start fresh.
    await put(api, "changing", fixed_window(5))
    response = await check(api, "changing", "carol")
    assert response.json()["allowed"] is True
    assert response.json()["limit"] == 5

async def test_invalid_input_gets_a_clear_problem_response(api: httpx.AsyncClient) -> None:
    bad_burst = {"algorithm": "FIXED_WINDOW", "bands": [{"limit": 5, "window": "PT1S", "burst": 2}]}
    response = await put(api, "bad", bad_burst)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"] == ["bands[0].burst is only supported by GCRA"]

    assert (await put(api, "Bad Name", fixed_window(5))).status_code == 400
    assert (await put(api, "bad-if-match", fixed_window(5), if_match="yesterday")).status_code == 400
    assert (await put(api, "typo", {"algorithm": "FIXED_WINDOW", "bandz": []})).status_code == 422

    assert (await check(api, "no-such-policy", "dave")).status_code == 404

    await put(api, "small", fixed_window(5))
    assert (await check(api, "small", "dave", cost=6)).status_code == 422
    assert (await check(api, "small", "")).status_code == 422

async def test_metrics_and_health_are_exposed(api: httpx.AsyncClient) -> None:
    await put(api, "metered", fixed_window(5))
    await check(api, "metered", "erin")

    metrics = await api.get("/metrics")
    assert metrics.status_code == 200
    assert 'ratekeeper_checks_total{policy="metered",outcome="allowed"} 1.0' in metrics.text

    health = await api.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "redis": "up", "postgres": "up", "redis_circuit": "closed"}

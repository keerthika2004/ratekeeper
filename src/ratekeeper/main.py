"""Application factory.


 

`create_app()` builds the FastAPI app; the lifespan function creates every

long-lived object once at startup (connection pools, cache, breaker,

services) and closes them at shutdown. Nothing connects at import time, so

the app starts even while Redis or Postgres are down and recovers on its own

once they're back.


 

Run with:  uvicorn ratekeeper.main:app

"""


 

from __future__ import annotations


 

import asyncio

import contextlib

import logging

from collections.abc import AsyncIterator

from functools import partial


 

import redis.asyncio as redis

from fastapi import FastAPI, Response

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from sqlalchemy.ext.asyncio import create_async_engine


 

from ratekeeper.api import health, routes

from ratekeeper.api.errors import install_error_handlers

from ratekeeper.check.breaker import CircuitBreaker, State

from ratekeeper.check.service import CheckService

from ratekeeper.config import Settings

from ratekeeper.metrics import Metrics

from ratekeeper.policies.cache import PolicyCache, listen_for_changes, publish_change

from ratekeeper.policies.service import PolicyService, load_policy

from ratekeeper.redis_limiter import RedisRateLimiter, TimeSource, redis_server_time


 

log = logging.getLogger(__name__)



 

def create_app(settings: Settings | None = None, *, now_us: TimeSource = redis_server_time) -> FastAPI:

    """`now_us` exists for tests; production always uses Redis' clock."""

    settings = settings or Settings()

    metrics = Metrics()


 

    @contextlib.asynccontextmanager

    async def lifespan(app: FastAPI) -> AsyncIterator[None]:

        # Check path: short timeouts, so a slow Redis can't hold requests hostage.

        redis_client = redis.from_url(

            settings.redis_url,

            socket_timeout=settings.redis_timeout_seconds,

            socket_connect_timeout=settings.redis_connect_timeout_seconds,

        )

        # Subscriptions sit idle between messages, so they need a connection

        # without the 100 ms read timeout (health checks keep it honest instead).

        pubsub_client = redis.from_url(

            settings.redis_url,

            socket_connect_timeout=settings.redis_connect_timeout_seconds,

            health_check_interval=30,

        )

        engine = create_async_engine(settings.database_url, pool_pre_ping=True)


 

        def on_state_change(old: State, new: State) -> None:

            log.warning("Redis circuit breaker: %s -> %s", old, new)

            metrics.circuit_state.state(new.value)


 

        breaker = CircuitBreaker(

            failure_rate_threshold=settings.breaker_failure_rate_threshold,

            window_size=settings.breaker_window_size,

            minimum_calls=settings.breaker_minimum_calls,

            open_seconds=settings.breaker_open_seconds,

            half_open_calls=settings.breaker_half_open_calls,

            on_state_change=on_state_change,

        )

        cache = PolicyCache(

            partial(load_policy, engine),

            refresh_after=settings.policy_cache_refresh_after_seconds,

            expire_after=settings.policy_cache_expire_after_seconds,

            max_size=settings.policy_cache_max_size,

        )


 

        async def on_policy_change(name: str) -> None:

            cache.invalidate(name)

            await publish_change(redis_client, name)


 

        app.state.redis = redis_client

        app.state.engine = engine

        app.state.breaker = breaker

        app.state.services = routes.Services(

            check=CheckService(

                cache, RedisRateLimiter(redis_client, now_us), breaker, metrics, settings.redis_timeout_seconds

            ),

            policies=PolicyService(engine, on_policy_change),

        )

        listener = asyncio.create_task(listen_for_changes(pubsub_client, cache))

        try:

            yield

        finally:

            listener.cancel()

            with contextlib.suppress(asyncio.CancelledError):

                await listener

            await redis_client.aclose()

            await pubsub_client.aclose()

            await engine.dispose()


 

    app = FastAPI(title="ratekeeper", version="0.1.0", lifespan=lifespan)

    app.include_router(routes.router)

    app.include_router(health.router)

    install_error_handlers(app)


 

    @app.get("/metrics", include_in_schema=False)

    async def prometheus_metrics() -> Response:

        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)


 

    app.state.metrics = metrics

    return app



 

app = create_app()


 
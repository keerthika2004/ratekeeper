"""Health endpoints.


 

/health/live   "the process is running" (restart me if this fails)

/health/ready  "send me traffic". Deliberately ignores Redis and Postgres:

               thanks to failure modes and the policy cache, this service can

               still answer while either is down. Marking every instance

               unready at once would turn a degraded service into no service.

/health        detailed status of each dependency, for humans and dashboards.

"""


 

from __future__ import annotations


 

import asyncio

from typing import Any


 

from fastapi import APIRouter, Request, Response, status

from sqlalchemy import text


 

router = APIRouter(prefix="/health")



 

@router.get("/live")

async def live() -> dict[str, str]:

    return {"status": "ok"}



 

@router.get("/ready")

async def ready() -> dict[str, str]:

    return {"status": "ok"}



 

@router.get("")

async def health(request: Request, response: Response) -> dict[str, Any]:

    state = request.app.state

    redis_up, postgres_up = await asyncio.gather(_redis_up(state.redis), _postgres_up(state.engine))

    if not (redis_up and postgres_up):

        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {

        "status": "ok" if redis_up and postgres_up else "degraded",

        "redis": "up" if redis_up else "down",

        "postgres": "up" if postgres_up else "down",

        "redis_circuit": state.breaker.state.value,

    }



 

async def _redis_up(client: Any) -> bool:

    try:

        async with asyncio.timeout(1):

            await client.ping()

        return True

    except Exception:

        return False



 

async def _postgres_up(engine: Any) -> bool:

    try:

        async with asyncio.timeout(2), engine.connect() as conn:

            await conn.execute(text("SELECT 1"))

        return True

    except Exception:

        return False


 
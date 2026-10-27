"""HTTP routes.

POST /v1/check consumes quota and answers 200 OK whenever the check itself
worked; whether the request was allowed is in the body. The caller is usually
a gateway or service, not the end user: the gateway turns allowed=false into a
429 for its own client and can copy the X-RateLimit-* and Retry-After headers
straight through.

Every response carrying a policy has an ETag equal to its version; send it
back as If-Match to update or delete safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from ratekeeper.api.errors import InvalidIfMatchError
from ratekeeper.api.schemas import CheckIn, CheckOut, HistoryOut, PolicyIn, PolicyOut, ceil_div
from ratekeeper.check.service import CheckResult, CheckService
from ratekeeper.policies.service import PolicyService

router = APIRouter(prefix="/v1")

@dataclass(frozen=True, slots=True)
class Services:
    """Built once at startup (see main.py) and stored on app.state."""

    check: CheckService
    policies: PolicyService

def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services

ServicesDep = Annotated[Services, Depends(_services)]
IfMatch = Annotated[str | None, Header(alias="If-Match")]

@router.post("/check")
async def check(body: CheckIn, response: Response, services: ServicesDep) -> CheckOut:
    result = await services.check.check(body.policy, body.identity, body.cost)
    response.headers.update(_rate_limit_headers(result))
    return CheckOut.of(result)

@router.put("/policies/{name}")
async def put_policy(
    name: str, body: PolicyIn, response: Response, services: ServicesDep, if_match: IfMatch = None
) -> PolicyOut:
    """Create (201) or replace (200, also when nothing changed). Safe to repeat."""
    result = await services.policies.put(
        name, body.algorithm, body.failure_mode, body.to_bands(), _parse_if_match(if_match)
    )
    if result.created:
        response.status_code = status.HTTP_201_CREATED
        response.headers["Location"] = f"/v1/policies/{name}"
    response.headers["ETag"] = f'"{result.policy.version}"'
    return PolicyOut.of(result.policy)

@router.get("/policies/{name}")
async def get_policy(name: str, response: Response, services: ServicesDep) -> PolicyOut:
    policy = await services.policies.get(name)
    response.headers["ETag"] = f'"{policy.version}"'
    return PolicyOut.of(policy)

@router.get("/policies")
async def list_policies(services: ServicesDep) -> list[PolicyOut]:
    return [PolicyOut.of(policy) for policy in await services.policies.list_all()]

@router.delete("/policies/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(name: str, services: ServicesDep, if_match: IfMatch = None) -> None:
    await services.policies.delete(name, _parse_if_match(if_match))

@router.get("/policies/{name}/history")
async def policy_history(
    name: str, services: ServicesDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[HistoryOut]:
    """Every change, most recent first, including deletions."""
    return [HistoryOut.of(entry) for entry in await services.policies.history(name, limit)]

def _rate_limit_headers(result: CheckResult) -> dict[str, str]:
    if result.decision is None:
        headers = {"X-RateLimit-Degraded": "true"}
        if not result.allowed:
            headers["Retry-After"] = "1"
        return headers
    band = result.decision.most_restrictive()
    headers = {
        "X-RateLimit-Limit": str(band.limit),
        "X-RateLimit-Remaining": str(band.remaining),
        "X-RateLimit-Reset": str(ceil_div(band.reset_after_us, 1_000_000)),
    }
    if not result.allowed:
        # Retry-After only supports whole seconds; round up so clients are never early.
        headers["Retry-After"] = str(max(1, ceil_div(result.decision.retry_after_us, 1_000_000)))
    return headers

def _parse_if_match(header: str | None) -> int | None:
    """Accepts "3", W/"3" or 3."""
    if header is None or not header.strip():
        return None
    value = header.strip().removeprefix("W/")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not (value.isascii() and value.isdigit()):  # isdigit() alone accepts "²"
        raise InvalidIfMatchError(header)
    return int(value)

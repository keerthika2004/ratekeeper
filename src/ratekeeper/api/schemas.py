"""Request and response bodies (Pydantic).


 

Pydantic only checks shape and types here. The real policy rules (ranges,

burst only for GCRA, ...) live in core/rules.py, so they apply no matter how

a policy gets created.


 

Windows are ISO-8601 durations ("PT1S", "PT1M", "PT1H", "P1D") so the unit is

never ambiguous. Pydantic parses and prints them as Python timedeltas.

"""


 

from __future__ import annotations


 

from datetime import datetime, timedelta


 

from pydantic import BaseModel, ConfigDict, Field, field_validator


 

from ratekeeper.check.service import CheckResult

from ratekeeper.core.models import Algorithm, Band, FailureMode, Policy

from ratekeeper.core.rules import NAME_PATTERN, InvalidPolicyError

from ratekeeper.policies.repository import Action, HistoryEntry


 

_ONE_US = timedelta(microseconds=1)



 

class _Strict(BaseModel):

    # Unknown fields are an error, so a typo like "limt" fails loudly instead of being ignored.

    model_config = ConfigDict(extra="forbid")



 

class BandIn(_Strict):

    limit: int

    window: timedelta

    burst: int | None = None


 

    def to_band(self) -> Band:

        micros = self.window // _ONE_US  # exact integer, no float rounding

        if micros % 1000:

            raise InvalidPolicyError([f"window must be a whole number of milliseconds: {self.window}"])

        return Band(self.limit, micros // 1000, self.burst)



 

class BandOut(BaseModel):

    limit: int

    window: timedelta

    burst: int | None


 

    @classmethod

    def of(cls, band: Band) -> BandOut:

        return cls(limit=band.limit, window=timedelta(milliseconds=band.window_ms), burst=band.burst)



 

class PolicyIn(_Strict):

    algorithm: Algorithm

    failure_mode: FailureMode | None = None

    """Defaults to OPEN."""

    bands: list[BandIn]


 

    def to_bands(self) -> list[Band]:

        return [band.to_band() for band in self.bands]



 

class PolicyOut(BaseModel):

    name: str

    version: int

    algorithm: Algorithm

    failure_mode: FailureMode

    bands: list[BandOut]


 

    @classmethod

    def of(cls, policy: Policy) -> PolicyOut:

        return cls(

            name=policy.name,

            version=policy.version,

            algorithm=policy.algorithm,

            failure_mode=policy.failure_mode,

            bands=[BandOut.of(band) for band in policy.bands],

        )



 

class HistoryOut(BaseModel):

    version: int

    action: Action

    algorithm: Algorithm

    failure_mode: FailureMode

    bands: list[BandOut]

    changed_at: datetime


 

    @classmethod

    def of(cls, entry: HistoryEntry) -> HistoryOut:

        return cls(

            version=entry.version,

            action=entry.action,

            algorithm=entry.spec.algorithm,

            failure_mode=entry.spec.failure_mode,

            bands=[BandOut.of(band) for band in entry.spec.bands],

            changed_at=entry.changed_at,

        )



 

class CheckIn(_Strict):

    """Callers name a policy; they can't send their own limits. Whoever owns the

    policy decides the limit, not whoever is being limited."""


 

    policy: str = Field(pattern=NAME_PATTERN)

    identity: str = Field(min_length=1, max_length=256, description="Who is limited: user id, API key, IP...")

    cost: int = Field(default=1, ge=1, le=1_000_000, description="Units to consume, e.g. 10 for a bulk call")


 

    @field_validator("identity")

    @classmethod

    def _not_blank(cls, value: str) -> str:

        if not value.strip():

            raise ValueError("must not be blank")

        return value



 

class CheckOut(BaseModel):

    """Times are milliseconds, rounded up so a client that waits retry_after_ms is never early.


 

    When `degraded` is true, Redis couldn't be asked: `allowed` comes from the

    policy's failure mode and the quota fields are null, because any number

    there would be made up.

    """


 

    allowed: bool

    degraded: bool

    policy: str

    identity: str

    limit: int | None

    remaining: int | None

    reset_after_ms: int | None

    retry_after_ms: int | None


 

    @classmethod

    def of(cls, result: CheckResult) -> CheckOut:

        if result.decision is None:

            return cls(

                allowed=result.allowed,

                degraded=True,

                policy=result.policy.name,

                identity=result.identity,

                limit=None,

                remaining=None,

                reset_after_ms=None,

                retry_after_ms=None,

            )

        band = result.decision.most_restrictive()

        return cls(

            allowed=result.allowed,

            degraded=False,

            policy=result.policy.name,

            identity=result.identity,

            limit=band.limit,

            remaining=band.remaining,

            reset_after_ms=ceil_div(band.reset_after_us, 1000),

            retry_after_ms=ceil_div(result.decision.retry_after_us, 1000),

        )



 

def ceil_div(a: int, b: int) -> int:

    return -(-a // b)


 
"""Domain types.


 

Frozen dataclasses rather than Pydantic models: the core has no framework

dependency, values can't be changed by accident after creation, and two

objects with the same fields compare equal (which the "repeated PUT is a

no-op" rule and the tests rely on). Pydantic is used only at the HTTP edge.

"""


 

from __future__ import annotations


 

from dataclasses import dataclass

from enum import StrEnum



 

class Algorithm(StrEnum):

    """See docs/ALGORITHMS.md for how each one works and when to pick it."""


 

    FIXED_WINDOW = "FIXED_WINDOW"

    """One counter per window; resets at each boundary. Cheap, but allows 2x bursts at window edges."""

    SLIDING_WINDOW_LOG = "SLIDING_WINDOW_LOG"

    """Remembers every admitted unit. Exact, but memory grows with the limit."""

    SLIDING_WINDOW_COUNTER = "SLIDING_WINDOW_COUNTER"

    """Weighted blend of the current and previous fixed window. O(1) memory, approximate."""

    GCRA = "GCRA"

    """Generic Cell Rate Algorithm: a token bucket stored as a single timestamp."""


 

    @property

    def script_name(self) -> str:

        return self.value.lower()


 

    @property

    def keys_per_band(self) -> int:

        return 2 if self is Algorithm.SLIDING_WINDOW_LOG else 1



 

class FailureMode(StrEnum):

    """What a check returns when Redis can't answer. Chosen per policy."""


 

    OPEN = "OPEN"

    """Allow. For limits where availability matters more (e.g. reading product pages)."""

    CLOSED = "CLOSED"

    """Reject. For limits guarding something costly or abusable (e.g. login, sending SMS)."""



 

@dataclass(frozen=True, slots=True)

class Band:

    """One "limit per window" rule, e.g. 10 per second.


 

    A policy can stack several bands (10/second AND 1000/hour); a request

    must fit all of them. ``burst`` is GCRA only and None for the others.

    """


 

    limit: int

    window_ms: int

    burst: int | None = None


 

    @property

    def window_us(self) -> int:

        return self.window_ms * 1000



 

@dataclass(frozen=True, slots=True)

class PolicySpec:

    """The editable part of a policy: everything except name and version."""


 

    algorithm: Algorithm

    failure_mode: FailureMode

    bands: tuple[Band, ...]



 

@dataclass(frozen=True, slots=True)

class Policy:

    """A named, versioned rate-limit configuration.


 

    The version is part of every Redis key (see keys.py), so editing a policy

    starts every identity on fresh counters instead of reinterpreting state

    written under different rules.

    """


 

    name: str

    version: int

    spec: PolicySpec


 

    @property

    def algorithm(self) -> Algorithm:

        return self.spec.algorithm


 

    @property

    def failure_mode(self) -> FailureMode:

        return self.spec.failure_mode


 

    @property

    def bands(self) -> tuple[Band, ...]:

        return self.spec.bands


 

    def capacity(self, band: Band) -> int:

        """Most units the band can hold: the burst for GCRA, the limit otherwise."""

        if self.algorithm is Algorithm.GCRA:

            assert band.burst is not None, "normalize() always sets a GCRA burst"

            return band.burst

        return band.limit


 

    @property

    def max_cost(self) -> int:

        """Largest cost a single request may have; anything above could never be admitted."""

        return min(self.capacity(band) for band in self.bands)



 

@dataclass(frozen=True, slots=True)

class BandResult:

    """The outcome of one band for one check.


 

    allowed:         whether this band alone had room for the request

    limit:           the band's capacity (burst for GCRA, limit otherwise)

    remaining:       units available right now, after this request if it was admitted

    reset_after_us:  time until the band is completely refilled (0 if it already is)

    retry_after_us:  if this band rejected, the exact wait before the same request fits; else 0

    """


 

    allowed: bool

    limit: int

    remaining: int

    reset_after_us: int

    retry_after_us: int



 

@dataclass(frozen=True, slots=True)

class Decision:

    """The result of checking one request against every band of a policy.


 

    Bands are all-or-nothing: the request is admitted only if every band has

    room, and then every band is charged. If any band rejects, none is charged.

    """


 

    allowed: bool

    bands: tuple[BandResult, ...]


 

    @property

    def retry_after_us(self) -> int:

        """The longest wait among the bands. Exact: a band with room now still has

        room later, because bands only refill while nothing is charged."""

        return max(band.retry_after_us for band in self.bands)


 

    def most_restrictive(self) -> BandResult:

        """The band to report in headers: when rejected, the one forcing the longest

        wait; when admitted, the one with the least room left (first one on ties)."""

        if self.allowed:

            return min(self.bands, key=lambda band: band.remaining)

        return max(self.bands, key=lambda band: band.retry_after_us)


 
"""Random policies and request sequences for property-based tests (Hypothesis).

Numbers are deliberately small (limits up to 20, windows of a few ms to a few
seconds) so random sequences actually hit the interesting cases: full bands,
rejections, window boundaries, partial refills.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import strategies as st

from ratekeeper.core.models import Algorithm, Band, Policy
from ratekeeper.core.rules import normalize

IDENTITIES = ["user-1", "user-2", "{evil}:tag", "ünïcødé 🚀"]
"""Includes characters that would break Redis keys if they weren't encoded."""

@dataclass(frozen=True, slots=True)
class Step:
    """One request in a scenario. raw_cost is reduced into [1, policy.max_cost]
    by cost(), since the valid range depends on the policy."""

    advance_us: int
    identity_index: int
    raw_cost: int

    @property
    def identity(self) -> str:
        return IDENTITIES[self.identity_index]

    def cost(self, policy: Policy) -> int:
        return 1 + (self.raw_cost - 1) % policy.max_cost

def _bands(algorithm: Algorithm) -> st.SearchStrategy[Band]:
    return st.builds(
        lambda limit, window_ms, burst, explicit: Band(
            limit, window_ms, burst if algorithm is Algorithm.GCRA and explicit else None
        ),
        st.integers(1, 20),
        st.one_of(st.integers(1, 50), st.integers(100, 5_000)),
        st.integers(1, 30),
        st.booleans(),
    )

@st.composite
def policies(draw: st.DrawFn, max_bands: int = 3) -> Policy:
    algorithm = draw(st.sampled_from(Algorithm))
    bands = draw(st.lists(_bands(algorithm), min_size=1, max_size=max_bands))
    return Policy("prop", 1, normalize(algorithm, None, bands))

_advance = st.one_of(
    st.just(0),  # same instant
    st.integers(1, 2_000),  # within a couple of ms
    st.integers(1, 200_000),  # within 200 ms
    st.integers(1, 6_000_000),  # up to 6 s
)
_cost = st.one_of(st.just(1), st.integers(1, 1_000))

scenarios = st.lists(
    st.builds(Step, _advance, st.integers(0, len(IDENTITIES) - 1), _cost), min_size=1, max_size=60
)

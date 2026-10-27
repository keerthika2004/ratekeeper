"""Small helpers shared by unit and integration tests."""


 

from __future__ import annotations


 

from ratekeeper.core.models import Algorithm, Band, Policy

from ratekeeper.core.rules import normalize


 

START_US = 1_786_000_000_000_000

"""Roughly mid-2026 in microseconds: realistic magnitude (~1.8 * 10**15), and a

whole number of seconds, so window boundaries in tests are easy to reason about."""



 

class ManualClock:

    """A clock tests move by hand, in microseconds. Callable, so it plugs in

    wherever a TimeSource (or the reference model's clock) is expected."""


 

    def __init__(self, now_us: int = START_US) -> None:

        self.now_us = now_us


 

    def __call__(self) -> int:

        return self.now_us


 

    def advance(self, micros: int) -> None:

        self.now_us += micros



 

def make_policy(algorithm: Algorithm, *bands: Band, name: str = "test", version: int = 1) -> Policy:

    return Policy(name, version, normalize(algorithm, None, bands))


 
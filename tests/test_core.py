"""Validation rules, decision aggregation and Redis key layout."""

from __future__ import annotations

import pytest

from ratekeeper.core.keys import redis_keys
from ratekeeper.core.models import Algorithm, Band, BandResult, Decision, FailureMode, Policy, PolicySpec
from ratekeeper.core.rules import InvalidPolicyError, normalize, validate_name

# --- rules -----------------------------------------------------------------

def test_normalize_fills_in_defaults() -> None:
    spec = normalize(Algorithm.GCRA, None, [Band(10, 1_000)])

    assert spec.failure_mode is FailureMode.OPEN
    assert spec.bands[0].burst == 10

def test_same_meaning_means_equal_specs() -> None:
    implicit = normalize(Algorithm.GCRA, None, [Band(10, 1_000)])
    explicit = normalize(Algorithm.GCRA, FailureMode.OPEN, [Band(10, 1_000, burst=10)])

    assert implicit == explicit

def test_reports_every_problem_at_once() -> None:
    with pytest.raises(InvalidPolicyError) as raised:
        normalize(Algorithm.FIXED_WINDOW, None, [Band(0, 1_000), Band(5, 0, burst=3)])

    assert raised.value.errors == [
        "bands[0].limit must be between 1 and 1000000",
        "bands[1].window must be between 1ms and 1 day",
        "bands[1].burst is only supported by GCRA",
    ]

def test_sliding_log_has_a_lower_limit_cap_because_it_stores_every_request() -> None:
    with pytest.raises(InvalidPolicyError):
        normalize(Algorithm.SLIDING_WINDOW_LOG, None, [Band(10_001, 1_000)])
    normalize(Algorithm.FIXED_WINDOW, None, [Band(10_001, 1_000)])

def test_gcra_horizon_is_bounded() -> None:
    # 1 per day with a burst of 11 would reach 11 days ahead.
    with pytest.raises(InvalidPolicyError):
        normalize(Algorithm.GCRA, None, [Band(1, 86_400_000, burst=11)])
    normalize(Algorithm.GCRA, None, [Band(1, 86_400_000, burst=10)])

def test_band_count_is_bounded() -> None:
    with pytest.raises(InvalidPolicyError):
        normalize(Algorithm.FIXED_WINDOW, None, [])
    with pytest.raises(InvalidPolicyError):
        normalize(Algorithm.FIXED_WINDOW, None, [Band(1, 1_000)] * 6)

@pytest.mark.parametrize("name", ["api", "login-v2", "a.b_c", "0"])
def test_accepts_plain_names(name: str) -> None:
    validate_name(name)

@pytest.mark.parametrize("name", ["", "API", "-leading", "has space", "has:colon", "brace{", "x" * 65, "api\n"])
def test_rejects_names_that_could_break_keys_or_metrics(name: str) -> None:
    with pytest.raises(InvalidPolicyError):
        validate_name(name)

# --- decisions -------------------------------------------------------------

def test_admitted_decision_reports_the_band_with_least_room() -> None:
    roomy = BandResult(True, 100, 80, 5, 0)
    tight = BandResult(True, 10, 2, 7, 0)
    decision = Decision(True, (roomy, tight))

    assert decision.most_restrictive() == tight
    assert decision.retry_after_us == 0

def test_rejected_decision_reports_the_band_forcing_the_longest_wait() -> None:
    short_wait = BandResult(False, 10, 0, 50, 50)
    long_wait = BandResult(False, 100, 0, 900, 900)
    fine = BandResult(True, 5, 3, 1, 0)
    decision = Decision(False, (short_wait, long_wait, fine))

    assert decision.most_restrictive() == long_wait
    assert decision.retry_after_us == 900

# --- keys ------------------------------------------------------------------

def _policy(name: str, version: int, algorithm: Algorithm, bands: int) -> Policy:
    band = Band(5, 1_000, 5 if algorithm is Algorithm.GCRA else None)
    return Policy(name, version, PolicySpec(algorithm, FailureMode.OPEN, (band,) * bands))

def test_one_key_per_band_with_shared_hash_tag() -> None:
    assert redis_keys(_policy("api", 3, Algorithm.GCRA, 2), "user-1") == [
        "rk:{api:user-1}:v3:b0",
        "rk:{api:user-1}:v3:b1",
    ]

def test_sliding_log_needs_a_log_and_a_sequence_per_band() -> None:
    assert redis_keys(_policy("api", 1, Algorithm.SLIDING_WINDOW_LOG, 1), "u") == [
        "rk:{api:u}:v1:b0:log",
        "rk:{api:u}:v1:b0:seq",
    ]

def test_identities_cannot_break_out_of_the_hash_tag() -> None:
    assert redis_keys(_policy("api", 1, Algorithm.FIXED_WINDOW, 1), "a}:{b") == ["rk:{api:a%7D%3A%7Bb}:v1:b0"]

def test_policies_and_versions_never_share_counters() -> None:
    login_v1 = set(redis_keys(_policy("login", 1, Algorithm.GCRA, 1), "same-user"))
    search_v1 = set(redis_keys(_policy("search", 1, Algorithm.GCRA, 1), "same-user"))
    login_v2 = set(redis_keys(_policy("login", 2, Algorithm.GCRA, 1), "same-user"))

    assert not login_v1 & search_v1
    assert not login_v1 & login_v2

"""Request parsing details that don't need a running app."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from ratekeeper.api.errors import InvalidIfMatchError
from ratekeeper.api.routes import _parse_if_match
from ratekeeper.api.schemas import BandIn, CheckIn
from ratekeeper.core.rules import InvalidPolicyError


def test_windows_are_iso_8601_durations() -> None:
    band = BandIn.model_validate({"limit": 5, "window": "PT1M30S"})

    assert band.to_band().window_ms == 90_000


def test_windows_must_be_whole_milliseconds() -> None:
    with pytest.raises(InvalidPolicyError):
        BandIn(limit=5, window=timedelta(microseconds=1500)).to_band()


def test_unknown_fields_are_rejected_instead_of_ignored() -> None:
    with pytest.raises(ValidationError):
        BandIn.model_validate({"limt": 5, "window": "PT1S"})


@pytest.mark.parametrize("identity", ["", "   ", "x" * 257])
def test_identity_must_be_meaningful(identity: str) -> None:
    with pytest.raises(ValidationError):
        CheckIn(policy="api", identity=identity)


def test_cost_defaults_to_one() -> None:
    assert CheckIn(policy="api", identity="u").cost == 1


@pytest.mark.parametrize(("header", "expected"), [(None, None), ("", None), ('"3"', 3), ('W/"3"', 3), ("3", 3)])
def test_if_match_formats(header: str | None, expected: int | None) -> None:
    assert _parse_if_match(header) == expected


@pytest.mark.parametrize("header", ["yesterday", '"-1"', '"²"', '"3'])
def test_if_match_garbage_is_rejected(header: str) -> None:
    with pytest.raises(InvalidIfMatchError):
        _parse_if_match(header)

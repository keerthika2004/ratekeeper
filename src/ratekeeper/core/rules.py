"""Validates and normalizes policies before they are stored.

Besides catching nonsense input, these bounds are what keep the Lua math
exact. Lua (inside Redis) stores every number as a 64-bit float, which holds
every whole number up to 2**53 (about 9 * 10**15) exactly and nothing reliably
above it. Each bound keeps every value, and every product the scripts compute,
below that line. (Python's own integers never overflow, so the reference
model doesn't need these bounds; Redis does.)
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ratekeeper.core.models import Algorithm, Band, FailureMode, PolicySpec

NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
"""Names appear inside Redis keys and metric labels, so keep them short and plain."""
_NAME = re.compile(NAME_PATTERN)

MAX_BANDS = 5

MAX_LIMIT = 1_000_000
"""The sliding counter computes limit * window_ms: 10**6 * 8.64 * 10**7 < 2**53."""

MAX_LOG_LIMIT = 10_000
"""The log stores one Redis entry per admitted unit, so its limit is also a memory bound per identity."""

MAX_WINDOW_MS = 86_400_000  # one day

MAX_BURST = 1_000_000

MAX_GCRA_HORIZON_US = 10 * 86_400_000_000
"""GCRA keeps a timestamp up to interval * burst ahead of now; 10 days keeps it far below 2**53 us."""


class InvalidPolicyError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("invalid policy: " + "; ".join(errors))
        self.errors = list(errors)


def validate_name(name: str) -> None:
    # fullmatch, not match: in Python, "$" also matches just before a trailing
    # newline, so match() would accept "api\n".
    if not _NAME.fullmatch(name):
        raise InvalidPolicyError(
            [f"name must match {NAME_PATTERN} (lowercase letters, digits, '.', '_', '-'; max 64 chars)"]
        )


def gcra_interval(window_us: int, limit: int) -> int:
    """Microseconds of quota one unit costs in GCRA, rounded UP so the sustained
    rate can never exceed limit/window (rounding down would admit slightly more)."""
    return -(-window_us // limit)


def normalize(algorithm: Algorithm, failure_mode: FailureMode | None, bands: Sequence[Band]) -> PolicySpec:
    """Return the spec with defaults filled in (failure mode OPEN, GCRA burst = limit),
    so two specs that mean the same thing are also equal. That equality is what
    makes a repeated PUT a no-op.

    Raises InvalidPolicyError listing every problem found, not just the first.
    """
    if not bands:
        raise InvalidPolicyError(["at least one band is required"])
    if len(bands) > MAX_BANDS:
        raise InvalidPolicyError([f"at most {MAX_BANDS} bands are allowed"])

    errors: list[str] = []
    normalized = tuple(_normalize_band(algorithm, band, f"bands[{i}]", errors) for i, band in enumerate(bands))
    if errors:
        raise InvalidPolicyError(errors)
    return PolicySpec(algorithm, failure_mode or FailureMode.OPEN, normalized)


def _normalize_band(algorithm: Algorithm, band: Band, path: str, errors: list[str]) -> Band:
    max_limit = MAX_LOG_LIMIT if algorithm is Algorithm.SLIDING_WINDOW_LOG else MAX_LIMIT
    limit_ok = 1 <= band.limit <= max_limit
    window_ok = 1 <= band.window_ms <= MAX_WINDOW_MS
    if not limit_ok:
        errors.append(f"{path}.limit must be between 1 and {max_limit}")
    if not window_ok:
        errors.append(f"{path}.window must be between 1ms and 1 day")

    burst = band.burst
    if algorithm is Algorithm.GCRA:
        if burst is None:
            burst = band.limit
        if not 1 <= burst <= MAX_BURST:
            errors.append(f"{path}.burst must be between 1 and {MAX_BURST}")
        elif limit_ok and window_ok and gcra_interval(band.window_us, band.limit) * burst > MAX_GCRA_HORIZON_US:
            errors.append(f"{path}: window / limit * burst must not exceed 10 days")
    elif burst is not None:
        errors.append(f"{path}.burst is only supported by GCRA")
    return Band(band.limit, band.window_ms, burst)

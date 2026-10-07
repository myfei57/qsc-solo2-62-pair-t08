"""Shift boundary clock.

Planned ownership changes take effect at the start of the next shift rather
than mid shift, so an operator never sees the attribution move under a
running shift. The default roster is the standard three eight hour shifts;
the boundary computation is pure so tests can pin any epoch second.
"""

from __future__ import annotations

import time

# Standard 3-shift roster, boundary hours in UTC, ascending.
DEFAULT_SHIFT_STARTS = (0, 8, 16)
DAY_SECONDS = 24 * 60 * 60
HOUR_SECONDS = 60 * 60


def floor_to_shift(at: int, shift_starts: tuple[int, ...] = DEFAULT_SHIFT_STARTS) -> int:
    """Return the start of the shift containing ``at``."""

    starts = _normalized_starts(shift_starts)
    day = (at // DAY_SECONDS) * DAY_SECONDS
    second_of_day = at - day
    current = starts[0] * HOUR_SECONDS
    for hour in starts:
        boundary = hour * HOUR_SECONDS
        if boundary <= second_of_day:
            current = boundary
        else:
            break
    return day + current


def next_shift_start(at: int, shift_starts: tuple[int, ...] = DEFAULT_SHIFT_STARTS) -> int:
    """Return the first shift boundary strictly after ``at``."""

    starts = _normalized_starts(shift_starts)
    day = (at // DAY_SECONDS) * DAY_SECONDS
    second_of_day = at - day
    for hour in starts:
        boundary = hour * HOUR_SECONDS
        if boundary > second_of_day:
            return day + boundary
    # After the last shift start: the next boundary is the first shift tomorrow.
    return day + DAY_SECONDS + starts[0] * HOUR_SECONDS


def shift_label(at: int, shift_starts: tuple[int, ...] = DEFAULT_SHIFT_STARTS) -> str:
    """Human readable label of the shift covering ``at``."""

    starts = _normalized_starts(shift_starts)
    start = floor_to_shift(at, starts)
    second_of_day = start - (start // DAY_SECONDS) * DAY_SECONDS
    index = starts.index(second_of_day // HOUR_SECONDS)
    return f"{_utc_date(start)}-shift{index + 1}"


def effective_time(at: int, effective: str, shift_starts: tuple[int, ...] = DEFAULT_SHIFT_STARTS) -> int:
    """Resolve the requested activation mode to an absolute effective time."""

    if effective == "emergency":
        return at
    if effective == "next_shift":
        return next_shift_start(at, shift_starts)
    raise ValueError("effective must be next_shift or emergency")


def _utc_date(at: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def _normalized_starts(shift_starts: tuple[int, ...]) -> tuple[int, ...]:
    if not shift_starts:
        raise ValueError("at least one shift start is required")
    starts = tuple(sorted(shift_starts))
    if any(hour < 0 or hour >= 24 for hour in starts):
        raise ValueError("shift start hours must lie inside a day")
    if len(set(starts)) != len(starts):
        raise ValueError("shift start hours must be unique")
    return starts

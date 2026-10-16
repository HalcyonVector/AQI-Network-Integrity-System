"""Shared date-window helper for the pulls whose upstream publishes late and irregularly.

Satellite AOD, ERA5 and OpenAQ's archive all arrive days after the fact, and by a
varying amount: MCD19A2 lagged 4 days when measured on 2026-10-12 and 7 days on
2026-10-16; OpenAQ's archive stalled for five days and then back-filled them a week
later. A single fixed "N days ago" date therefore silently misses data. Re-processing
a trailing window each run self-heals, and is safe because the outputs are one file
per observation date, overwritten on re-run.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def trailing_window(now: datetime, newest_days_back: int, span_days: int) -> list[datetime]:
    """Midnight datetimes from (now - newest_days_back - span_days + 1) to
    (now - newest_days_back), oldest first, e.g. now=Oct 16, newest=3, span=3 ->
    [Oct 11, Oct 12, Oct 13]."""
    if span_days < 1 or newest_days_back < 0:
        raise ValueError("span_days must be >= 1 and newest_days_back >= 0")
    base = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    newest = base - timedelta(days=newest_days_back)
    return [newest - timedelta(days=i) for i in range(span_days - 1, -1, -1)]

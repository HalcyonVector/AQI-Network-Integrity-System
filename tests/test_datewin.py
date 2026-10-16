import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from datewin import trailing_window  # noqa: E402


def test_window_is_oldest_first_and_ends_at_newest_days_back():
    out = trailing_window(datetime(2026, 10, 16, 13, 45), newest_days_back=3, span_days=3)
    assert out == [datetime(2026, 10, 11), datetime(2026, 10, 12), datetime(2026, 10, 13)]


def test_window_normalises_time_of_day_and_timezone():
    now = datetime(2026, 10, 16, 23, 59, 59, tzinfo=timezone.utc)
    out = trailing_window(now, 0, 1)
    assert out == [datetime(2026, 10, 16)]
    assert out[0].tzinfo is None


def test_window_crosses_month_boundary():
    out = trailing_window(datetime(2026, 11, 2), newest_days_back=2, span_days=4)
    assert [d.strftime("%Y-%m-%d") for d in out] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"]


def test_window_rejects_nonsense_arguments():
    with pytest.raises(ValueError):
        trailing_window(datetime(2026, 10, 16), 3, 0)
    with pytest.raises(ValueError):
        trailing_window(datetime(2026, 10, 16), -1, 3)

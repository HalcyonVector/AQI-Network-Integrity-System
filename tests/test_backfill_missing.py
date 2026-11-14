import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_missing as bf  # noqa: E402


def _touch(raw, prefix, days):
    for d in days:
        (raw / f"{prefix}_{d}.csv").write_text("x")


def test_holes_only_between_first_and_newest(tmp_path):
    _touch(tmp_path, "openaq_hourly", ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"])
    assert bf.holes("openaq_hourly", tmp_path) == [date(2026, 10, 3), date(2026, 10, 4)]
    assert bf.holes("firms_acq", tmp_path) == []  # nothing there: no basis for a hole


def test_runs_of_days_groups_contiguous():
    d = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
    assert bf.runs_of_days(d) == [(date(2026, 10, 1), date(2026, 10, 2)), (date(2026, 10, 5), date(2026, 10, 5))]


def test_commands_use_range_or_single_date_and_cap():
    days = [date(2026, 10, i) for i in range(1, 11)]
    aod = bf.commands("aod_ncr", days)
    assert len(aod) == bf.MAX_DAYS_PER_RUN["aod_ncr"] and "--date" in aod[0]
    assert "2026-10-10" in aod[0]  # newest first
    oq = bf.commands("openaq_hourly", days)
    assert len(oq) == 1 and oq[0][-4:] == ["--start", "2026-10-01", "--end", "2026-10-10"]

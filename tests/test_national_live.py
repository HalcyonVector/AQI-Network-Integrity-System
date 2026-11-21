import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_national_live as an  # noqa: E402
import dashboard_national as dn  # noqa: E402


def _hours(n=48):
    return pd.date_range("2026-11-01", periods=n, freq="h")


def test_clean_removes_out_of_range_and_stuck_runs():
    idx = _hours(24)
    a = pd.Series(np.arange(24, dtype=float) + 10, index=idx)
    a.iloc[3] = 5000.0           # glitch
    a.iloc[10:18] = 77.0         # 8 identical hours: stuck
    out, st = an.clean(pd.DataFrame({1: a}))
    assert pd.isna(out[1].iloc[3]) and out[1].iloc[10:18].isna().all()
    assert st["removed_range"] == 1 and st["removed_flatline"] == 8 and st["hours_clean"] == 15


def test_daily_summary_needs_18_hours_and_station_status_reports_level():
    idx = _hours(48)
    full = pd.Series(np.linspace(40, 60, 48), index=idx)
    thin = full.copy(); thin.iloc[:40] = np.nan  # 8 hours on day 2 only
    pm = pd.DataFrame({1: full, 2: thin})
    d = an.daily_summary(pm)
    assert list(d["stations"]) == [1, 1] and d["median_pm25"].notna().all()
    master = pd.DataFrame({"openaq_id": [1, 2], "name": ["A", "B"], "city": ["X", "X"], "state": ["S", "S"], "latitude": [1.0, 2.0], "longitude": [1.0, 2.0]})
    st = an.station_status(pm, pd.DataFrame(), master)
    assert set(st["level"]) == {"none"} and st["name"].tolist() == ["A", "B"]
    assert an.city_latest(st, min_stations=1)["stations"].iloc[0] >= 1


def test_national_html_escapes_and_degrades():
    assert dn.national_html(None, None, None, None, None) == ""
    status = pd.DataFrame({"openaq_id": [1], "name": ["<b>X</b>"], "latitude": [20.0], "longitude": [78.0], "level": ["flag"], "latest_day": ["2026-11-15"]})
    snap = pd.DataFrame({"station": ["<i>S</i>"], "latitude": [20.0], "longitude": [78.0], "pm25_ugm3": [95.0], "censored": [False]})
    out = dn.national_html(snap, status, None, None, None)
    assert "&lt;b&gt;X" in out and "&lt;i&gt;S" in out and "<b>X</b>" not in out and "<i>S</i>" not in out
    assert "Not enough days" in out and "15 Nov 2026" in out
    assert dn.band(95)[1] == "91 to 120" and dn.band(1000)[1] == "above 250"

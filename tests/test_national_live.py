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
    assert "&lt;i&gt;S" in out and "<i>S</i>" not in out
    stn = dn.national_stations_html(status.assign(state="S", completeness=1.0, failing_share=0.1, reason="x", city="C"))
    assert "&lt;b&gt;X" in stn and "<b>X</b>" not in stn and "S: 1 station" in stn
    assert "Not enough days" in out and "15 Nov 2026" in out
    assert dn.band(95)[1] == "91 to 120" and dn.band(1000)[1] == "above 250"


def test_satellite_groups_and_global_fdr():
    import analyze_national_satellite as sat
    coords = pd.DataFrame({"latitude": [28.6, 28.7, 28.5, 13.0, 13.1], "longitude": [77.2, 77.1, 77.3, 80.2, 80.3]}, index=["1", "2", "3", "4", "5"])
    g = sat.regional_groups(coords)
    assert g["1"] == g["2"] == g["3"] and g["4"] == g["5"] and g["1"] != g["4"]
    q = sat.benjamini_hochberg(np.array([0.001, 0.02, 0.5, 0.9]))
    assert q[0] < 0.01 and q[3] <= 1.0 and (np.diff(q[np.argsort([0.001, 0.02, 0.5, 0.9])]) >= 0).all()


def test_satellite_run_flags_the_station_that_does_not_track_and_gates_a_region_with_no_signal():
    import analyze_national_satellite as sat
    rng = np.random.default_rng(1)
    days = [f"2026-10-{d:02d}" for d in range(1, 21)]
    base = np.exp(rng.normal(0, 0.6, len(days)))
    rows_g, rows_a = [], []
    for i in range(8):
        sid = str(i)
        noise = rng.normal(0, 0.05, len(days))
        pm = 50 * base * np.exp(noise) if i != 7 else np.exp(rng.normal(3.5, 0.6, len(days)))  # station 7 ignores the satellite
        rows_g += [{"station": sid, "date": d, "pm25": float(v)} for d, v in zip(days, pm)]
        rows_a += [{"station": sid, "date": d, "aod_055": float(v)} for d, v in zip(days, base)]
    groups = pd.Series({str(i): 1 for i in range(8)})
    st, g = sat.run(pd.DataFrame(rows_g), pd.DataFrame(rows_a), groups)
    assert bool(st.set_index("station").loc["7", "flag"]) and int(st["flag"].sum()) == 1 and not bool(g["gated"].iloc[0])
    flat = pd.DataFrame(rows_a).assign(aod_055=lambda d: rng.normal(1, 0.5, len(d)).clip(0.1))  # satellite unrelated to ground everywhere
    st2, g2 = sat.run(pd.DataFrame(rows_g), flat, groups)
    assert not st2["flag"].any() and bool(g2["gated"].iloc[0])

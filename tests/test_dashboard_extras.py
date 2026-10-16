import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard_extras as ex  # noqa: E402


def _feed():
    return pd.DataFrame({
        "station": ["A <b>", "B", "C"], "city": ["Delhi", "Delhi", "Noida"],
        "latitude": [28.6, 28.7, 28.5], "longitude": [77.1, 77.2, 77.4],
        "PM2.5": [40.0, 180.0, None], "PM10": [60.0, 200.0, 90.0],
    })


def test_band_boundaries():
    assert ex.band(50)[1] == "Good"
    assert ex.band(51)[1] == "Satisfactory"
    assert ex.band(401)[1] == "Severe"


def test_map_marks_flags_escapes_and_handles_missing_pm():
    health = pd.DataFrame({"station": ["B"], "level": ["flag"]})
    svg = ex.station_map_svg(_feed(), health)
    assert svg.count("<circle") >= 4 and "#f43f5e" in svg  # ring for the flagged station
    assert "<b>" not in svg and "A &lt;b&gt;" in svg
    assert "n/a" in svg  # station C has no PM2.5
    assert "not available" in ex.station_map_svg(None, None)


def test_ranking_and_cities_degrade_and_escape():
    assert "A &lt;b&gt;" in ex.ranking_html(_feed()) or "&lt;" in ex.ranking_html(_feed())
    assert "Noida" not in ex.city_cards_html(_feed())  # its only station has no PM2.5, so no card
    assert "Delhi" in ex.city_cards_html(_feed())
    assert "not available" in ex.ranking_html(pd.DataFrame())


def _filled():
    hours = pd.date_range("2026-10-01", periods=48, freq="h")
    rows = []
    for st, off in (("S1", 0.0), ("S2", 30.0)):
        for i, h in enumerate(hours):
            src = "observed" if i < 40 else "unfilled"
            rows.append({"hour_start_ist": str(h), "station": st, "pm25_final": (50 + off) if src == "observed" else None, "source": src})
    return pd.DataFrame(rows)


def test_heatmap_has_cell_per_station_day_and_marks_empty():
    f = _filled()
    f = f[~((f["station"] == "S2") & (f["hour_start_ist"].str.startswith("2026-10-02")))]  # S2 has no data on day 2
    svg = ex.heatmap_svg(f)
    assert svg.count("<rect") == 4  # 2 stations x 2 days
    assert svg.count('class="hole"') == 1 and "S2 2026-10-02: no data" in svg
    assert "not available" in ex.heatmap_svg(None)


def test_completeness_and_diurnal():
    html_ = ex.completeness_html(_filled())
    assert "observed" in html_ and "still missing" in html_
    d = ex.diurnal_values(_filled())
    assert d is not None and len(d) == 24
    assert ex.diurnal_values(pd.DataFrame()) is None


def test_scatter_needs_enough_points():
    assert "needs at least 4" in ex.scatter_svg([1, 2], [1, 2], ["a", "b"], "x", "y")
    out = ex.scatter_svg([1, 2, 3, 4], [2, 4, 6, 8], list("abcd"), "x", "y")
    assert out.count("<circle") == 4 and "+1.00" in out


def test_dry_day_counter_uses_analysis_window_when_present():
    d = pd.DataFrame({
        "pm25_delhi": [10.0] * 6, "hit_frp_925": [1.0] * 6,
        "washout": [False, False, True, False, False, False],
        "in_analysis_window": [False, True, True, True, True, True],
    })
    out = ex.dry_day_counter(d)
    assert "4 / 20" in out  # excludes the out-of-window day and the wet day
    assert "16 more" in out
    assert "not available" in ex.dry_day_counter(None)


def test_countdown_states():
    assert "passed" in ex.countdown_html(date(2027, 1, 1))
    assert "10 d" in ex.countdown_html(date(2026, 10, 16))


def test_parse_run_log_and_html(tmp_path):
    log = ("2026-10-16 12:00:01 Starting CPCB fetch\n2026-10-16 12:01:00 fetch_aod.py exited with code 1\n"
           "2026-10-16 12:20:00 Committed and pushed\n2026-10-16 19:00:00 Starting CPCB fetch\n2026-10-16 19:10:00 Committed and pushed\n"
           "2026-10-17 12:00:00 Starting CPCB fetch\n")
    runs = ex.parse_run_log(log)
    assert [r["failed"] for r in runs] == [["fetch_aod"], [], []]
    assert runs[1]["committed"] and not runs[2]["committed"]
    (tmp_path / "run_and_push_2026-10-16.log").write_text(log, encoding="utf-8")
    out = ex.run_log_html(tmp_path)
    assert "FAILED STEPS" in out and "INCOMPLETE" in out and "fetch_aod" in out
    assert "No run logs" in ex.run_log_html(tmp_path / "missing")


def test_sparkline_needs_two_points():
    assert "Not enough data" in ex.sparkline_card("t", ["2026-10-01"], [1.0], "u", "#fff")
    assert "<polyline" in ex.sparkline_card("t", ["2026-10-01", "2026-10-02"], [1.0, 2.0], "u", "#fff")

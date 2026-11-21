import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard_historical as dh  # noqa: E402


def _master():
    return pd.DataFrame({"cpcb_id": ["a", "b", "c"], "name": ["Alpha <b>", "Beta", "Gamma"], "city": ["Delhi", "Delhi", "Pune"], "state": ["Delhi", "Delhi", "Maharashtra"],
                         "latitude": [28.6, 28.7, 18.5], "longitude": [77.2, 77.1, 73.8], "years_with_pm25": [5, 3, 1]})


def _audit():
    return pd.DataFrame({"station_id": ["a", "b", "c", "a"], "year": [2025, 2025, 2025, 2024], "level": ["flag", "ok", "none", "flag"],
                         "drifting": [True, False, False, True], "trend_log_per_day": [0.03, 0.0, 0.0, 0.02]})


def test_backfill_cards_and_table():
    qc = pd.DataFrame({"year": [2024], "hours_raw": [1000], "removed_range": [0], "removed_flatline": [10], "hours_clean": [990], "stations": [5]})
    out = dh.backfill_html(qc, [("CPCB", 11, 11, "ok"), ("ERA5", 0, 6, "<queued>")])
    assert "complete" in out and "waiting" in out and "&lt;queued&gt;" in out and "990" in out and "1.0%" in out
    assert "not available" in dh.backfill_html(None, [])


def test_completeness_matrix_marks_empty_cells():
    comp = pd.DataFrame({"state": ["Delhi", "Delhi", "Goa"], "month": ["2019-01", "2019-03", "2019-01"], "stations": [3, 3, 1], "completeness": [0.9, 0.5, 0.2]})
    svg = dh.completeness_matrix_svg(comp)
    assert svg.count("<rect") == 6 and svg.count('class="hole"') == 3  # 2 states x 3 months (Jan to Mar), three of them reported
    assert "Delhi 2019-01: 90% of hours" in svg and "not available" in dh.completeness_matrix_svg(None)


def test_india_map_escapes_colours_by_level_and_handles_missing():
    svg = dh.india_map_svg(_master(), _audit(), 2025)
    assert svg.count("<circle") == 3 and "Alpha &lt;b&gt;" in svg and "<b>" not in svg
    assert dh.LEVEL_COLOUR["flag"] in svg and "cannot be checked" in svg
    assert "No audited stations" in dh.india_map_svg(_master(), _audit(), 1999) and "not available" in dh.india_map_svg(None, None, 2025)


def test_league_ranks_worst_first_and_drops_small_cities():
    lg = pd.DataFrame({"city": ["A", "B", "C"], "state": ["s"] * 3, "year": [2025] * 3, "stations": [5, 6, 2], "stations_audited": [4, 5, 1],
                       "failing_share": [0.02, 0.09, 0.5], "flagged": [1, 2, 1], "drifting": [0, 1, 0], "outage_share": [0.1, 0.2, 0.3]})
    out = dh.league_html(lg, 2025)
    assert out.index("B<span") < out.index("A<span") and "C<span" not in out and "9.0%" in out
    assert "No city" in dh.league_html(lg, 2025, min_audited=99)


def test_season_multiples_and_drift_watchlist():
    delhi = pd.DataFrame({"date": ["2019-10-01", "2019-10-02", "2020-10-01"], "pm25": [100.0, 200.0, 50.0], "stations": [30, 30, 30]})
    fires = pd.DataFrame({"date": ["2019-10-01", "2020-10-01"], "source": ["VIIRS_SNPP_SP"] * 2, "detections": [500, 100], "frp_sum": [1.0, 1.0]})
    svg = dh.season_multiples_svg(delhi, fires, [2019, 2020])
    assert svg.count("<polyline") == 2 and svg.count("<rect") == 2 and "500 fire detections" in svg
    assert "not available" in dh.season_multiples_svg(None, None, [2019])
    wl = dh.drift_watchlist_html(_audit(), _master())
    assert "Alpha &lt;b&gt;" in wl and "2024, 2025" in wl and "Beta" not in wl
    assert "unresolved: still drifting in 2025" in wl and "over 2025" in wl
    assert "No station" in dh.drift_watchlist_html(_audit().assign(drifting=False), _master())


def test_drift_status_is_cleared_when_later_years_are_clean_and_unresolved_without_data():
    a = pd.DataFrame({"station_id": ["a", "a", "b", "c"], "year": [2022, 2023, 2022, 2023], "level": ["flag", "ok", "flag", "ok"],
                      "drifting": [True, False, True, False], "trend_log_per_day": [0.02, 0.0, -0.03, 0.0], "trend_days": [300, 300, 200, 200]})
    m = pd.DataFrame({"cpcb_id": ["a", "b", "c"], "name": ["A", "B", "C"], "city": ["X"] * 3})
    wl = dh.drift_watchlist_html(a, m)
    assert "cleared: not drifting since" in wl and "unresolved: no later data to check" in wl
    assert "+" in wl and "-" in wl  # drift can go either way

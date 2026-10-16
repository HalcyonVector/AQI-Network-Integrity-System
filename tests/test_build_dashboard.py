import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_dashboard as bd  # noqa: E402

TODAY = date(2026, 10, 16)


def d(day):
    return date(2026, 10, day)


def test_source_is_ok_when_within_its_normal_lag_plus_grace():
    assert bd.source_status([d(14), d(15)], lag_days=1, today=TODAY)["status"] == "ok"
    assert bd.source_status([d(9)], lag_days=7, today=TODAY)["status"] == "ok"  # 7 days old but 7 is its normal lag


def test_fresh_source_with_missing_days_behind_it_is_gaps_not_ok():
    # Regression: the CPCB feed showed a green OK beside 18 missing days, because the chip only
    # looked at how new the newest file was.
    s = bd.source_status([d(5), d(6), d(14), d(15)], lag_days=1, today=TODAY)
    assert s["status"] == "gaps" and len(s["holes"]) > 0
    assert bd.source_status([d(13), d(14), d(15)], lag_days=1, today=TODAY)["status"] == "ok"  # contiguous: genuinely fine


def test_a_stale_source_stays_stale_even_when_it_also_has_holes():
    assert bd.source_status([d(5), d(7)], lag_days=1, today=TODAY)["status"] == "stale"  # 9 days old, hole on the 6th


def test_source_goes_stale_then_down_as_it_ages_past_its_lag():
    assert bd.source_status([d(5)], lag_days=1, today=TODAY)["status"] == "stale"  # 11 days old, lag 1
    assert bd.source_status([date(2026, 9, 25)], lag_days=1, today=TODAY)["status"] == "down"  # 21 days


def test_source_with_no_files_is_reported_not_crashed():
    s = bd.source_status([], lag_days=1, today=TODAY)
    assert s["status"] == "none" and s["latest"] is None and s["holes"] == []


def test_holes_are_the_missing_dates_between_first_and_newest():
    s = bd.source_status([d(10), d(11), d(14), d(15)], lag_days=1, today=TODAY)
    assert s["holes"] == [d(12), d(13)]


def test_holes_only_look_back_thirty_days():
    dates = [date(2026, 8, 1), d(14), d(15)]  # a long-ago file must not make 60 days of "holes"
    holes = bd.source_status(dates, lag_days=1, today=TODAY)["holes"]
    assert all((d(15) - h).days <= bd.HOLE_WINDOW_DAYS for h in holes)


def test_file_dates_parses_names_and_ignores_unrelated_files(tmp_path):
    for name in ("cpcb_ncr_2026-10-14.csv", "cpcb_ncr_2026-10-15.csv", "cpcb_ncr_notes.csv", "other_2026-10-14.csv"):
        (tmp_path / name).write_text("x")
    assert bd.file_dates("cpcb_ncr_*.csv", tmp_path) == [d(14), d(15)]


def test_line_bar_svg_draws_a_point_and_a_bar_per_value_with_tooltips():
    svg = bd.line_bar_svg(["2026-10-01", "2026-10-02", "2026-10-03"], [10.0, 20.0, 30.0], [5.0, 0.0, 15.0], "PM", "Exposure")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<circle") == 3 and svg.count("<rect") == 3 and svg.count("<polyline") == 1
    assert "2026-10-02: 20.0" in svg


def test_line_bar_svg_breaks_the_line_at_missing_values():
    svg = bd.line_bar_svg([f"2026-10-0{i}" for i in range(1, 6)], [10.0, 11.0, None, 13.0, 14.0], [1.0] * 5, "PM", "E")
    assert svg.count("<polyline") == 2  # two segments, not a line drawn across the gap
    assert svg.count("<circle") == 4


def test_line_bar_svg_handles_empty_and_all_zero_input():
    assert "No data" in bd.line_bar_svg([], [], [], "a", "b")
    assert "<svg" in bd.line_bar_svg(["2026-10-01"], [0.0], [0.0], "a", "b")


def test_station_names_and_reasons_are_html_escaped():
    health = pd.DataFrame({"station": ['<script>alert(1)</script> & Co'], "level": ["flag"], "reason": ['"<b>bad</b>"']})
    out = bd.stations_html(health)
    assert "<script>" not in out and "<b>bad</b>" not in out
    assert "&lt;script&gt;" in out and "&amp; Co" in out


def test_stations_html_only_lists_flagged_and_watch():
    health = pd.DataFrame({"station": ["A", "B", "C"], "level": ["flag", "watch", "ok"], "reason": ["r1", "r2", "fine"]})
    out = bd.stations_html(health)
    assert "A" in out and "B" in out and ">C<" not in out


def test_stations_html_degrades_gracefully():
    assert "not available" in bd.stations_html(None)
    assert "No stations flagged" in bd.stations_html(pd.DataFrame({"station": ["A"], "level": ["ok"], "reason": ["x"]}))


def test_decoupling_html_counts_flagged_and_borderline():
    df = pd.DataFrame({"station": ["A", "B", "C"], "n_days": [9, 9, 9], "rho": [-0.3, 0.6, 0.55], "network_median_rho": [0.6] * 3,
                       "q_value": [0.1, 0.9, 0.9], "flag": [False, False, False],
                       "reason": ["watch: looks decoupled", "tracks satellite like the network", "tracks satellite like the network"]})
    out = bd.decoupling_html(df)
    assert "0 flagged" in out and "1 borderline" in out and "A" in out and ">B<" not in out


def test_render_has_all_sections_and_flags_an_unhealthy_source(tmp_path):
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    raw.mkdir()
    proc.mkdir()
    (raw / "cpcb_ncr_2026-09-01.csv").write_text("x")  # 45 days old: DOWN
    page = bd.render(TODAY, raw, proc)
    assert page.startswith("<!doctype html>") and "</html>" in page
    assert "Is the data flowing?" in page and "Station health" in page and "Agreement with the satellite" in page
    assert "GAPS" in bd.render(date(2026, 10, 16), *_gappy(tmp_path))
    assert "CPCB feed (data.gov.in) is DOWN" in page
    assert "<script" not in page  # a static page: no JavaScript anywhere
    assert "http://" not in page and "https://" not in page  # and no external requests


def test_render_works_with_every_input_missing(tmp_path):
    page = bd.render(TODAY, tmp_path, tmp_path)
    assert "NO DATA" in page and "not available" in page


def test_stubble_chart_uses_the_first_available_exposure_column(tmp_path):
    proc = tmp_path
    pd.DataFrame({"date": ["2026-10-01", "2026-10-02"], "pm25_delhi": [40.0, 50.0], "hit_frp_k1.0": [1.0, 2.0]}).to_csv(proc / "stubble_transport_daily.csv", index=False)
    out = bd.stubble_html(bd._read("stubble_transport_daily.csv", proc))
    assert out.count("<circle") == 2
    pd.DataFrame({"date": ["2026-10-01"], "pm25_delhi": [40.0], "other": [1.0]}).to_csv(proc / "stubble_transport_daily.csv", index=False)
    assert "not available" in bd.stubble_html(bd._read("stubble_transport_daily.csv", proc))


def _gappy(tmp_path):
    raw, proc = tmp_path / "raw2", tmp_path / "proc2"
    raw.mkdir()
    proc.mkdir()
    for day in (10, 11, 14, 15):
        (raw / f"cpcb_ncr_2026-10-{day}.csv").write_text("x")
    return raw, proc


def _points(svg):
    import re
    return [(float(a), float(b)) for a, b in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', svg)]


def _bars(svg):
    import re
    return [(float(y), float(h)) for y, h in re.findall(r'<rect x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg)]


def test_chart_geometry_higher_values_are_higher_on_screen_and_stay_inside_the_plot():
    svg = bd.line_bar_svg([f"2026-10-0{i}" for i in range(1, 6)], [10.0, 40.0, 25.0, 5.0, 30.0], [1.0, 9.0, 4.0, 0.0, 6.0], "PM", "E", width=760, height=280)
    pts = _points(svg)
    assert len(pts) == 5
    ys = [y for _, y in pts]
    assert ys.index(min(ys)) == 1 and ys.index(max(ys)) == 3  # 40 is the highest point, 5 the lowest (SVG y grows downward)
    xs = [x for x, _ in pts]
    assert xs == sorted(xs)  # time runs left to right
    top, bottom = 18, 280 - 44  # plot area used by the chart
    assert all(top <= y <= bottom for y in ys)
    assert all(52 <= x <= 760 - 52 for x in xs)


def test_chart_geometry_bar_heights_are_proportional_and_sit_on_the_baseline():
    svg = bd.line_bar_svg(["2026-10-01", "2026-10-02", "2026-10-03"], [1.0, 1.0, 1.0], [2.0, 4.0, 8.0], "PM", "E", width=760, height=280)
    bars = _bars(svg)
    heights = [h for _, h in bars]
    assert heights[1] / heights[0] == pytest.approx(2.0, rel=0.02) and heights[2] / heights[1] == pytest.approx(2.0, rel=0.02)
    baseline = 280 - 44
    assert all(y + h == pytest.approx(baseline, abs=0.2) for y, h in bars)  # every bar stands on the x axis

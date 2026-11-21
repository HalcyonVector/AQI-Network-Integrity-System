import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "historical"))
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_era5_history as eh  # noqa: E402
import station_master as sm  # noqa: E402


def _src(rows):
    return pd.DataFrame(rows, columns=sm.SOURCE_COLS)


def test_station_id_is_stable_and_keeps_the_agency():
    a = sm.station_id("Delhi", "Delhi", "Bawana, Delhi - DPCC")
    assert a == "delhi_delhi_bawana-delhi-dpcc" and a == sm.station_id("Delhi", "Delhi", "Bawana, Delhi - DPCC")
    assert a != sm.station_id("Delhi", "Delhi", "Bawana, Delhi - IITM")


def test_match_by_coordinates_then_by_name_and_never_double_claims():
    feed = _src([("f1", "Bawana, Delhi - DPCC", "Delhi", "Delhi", 28.7762, 77.0511),
                 ("f2", "Rohini, Delhi - DPCC", "Delhi", "Delhi", 28.7325, 77.1199)])
    other = _src([("p1", "BAWANA", "Delhi", "Delhi", 28.7763, 77.0512),     # ~15 m from f1: coordinate match
                  ("p2", "Rohini", "Delhi", "Delhi", None, None),            # no coordinates: name fallback
                  ("p3", "Bawana annex", "Delhi", "Delhi", 28.7764, 77.0513)])  # f1 already claimed: its own row
    m = sm.build_master({"feed": feed, "princeton": other})
    assert len(m) == 3
    row = m.set_index("feed_id")
    assert row.loc["f1", "princeton_id"] == "p1" and row.loc["f2", "princeton_id"] == "p2"
    assert m[m["princeton_id"] == "p3"]["feed_id"].isna().all()
    assert sorted(m["n_sources"]) == [1, 2, 2]


def test_distant_same_name_is_not_matched_by_coordinates_but_by_name_only_in_same_city():
    feed = _src([("f1", "Sector 62, Noida - IITM", "Noida", "Uttar Pradesh", 28.62, 77.36)])
    far = _src([("p1", "Sector 62", "Ghaziabad", "Uttar Pradesh", 28.67, 77.44)])  # other city, 9 km away
    assert len(sm.build_master({"feed": feed, "princeton": far})) == 2


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError):
        sm.build_master({"feed": pd.DataFrame({"name": ["x"]})})


def test_duplicate_full_names_get_distinct_ids():
    feed = _src([("a", "Same, X - A", "X", "S", 10.0, 10.0), ("b", "Same, X - A", "X", "S", 20.0, 20.0)])
    m = sm.build_master({"feed": feed})
    assert m["station_id"].is_unique


def test_era5_requests_mirror_the_live_pipeline():
    ds, single = eh.build_request("single", 11, [2019, 2020])
    assert ds == eh.live.DATASET and len(single["day"]) == 30 and single["area"] == eh.live.AREA
    assert single["variable"] == eh.live.VARIABLES and single["time"] == eh.live.HOURS and single["year"] == ["2019", "2020"]
    ds, pr = eh.build_request("pressure", 2, [2020])
    assert ds == eh.live.PRESSURE_DATASET and len(pr["day"]) == 29 and pr["pressure_level"] == [eh.live.PRESSURE_LEVEL]
    assert len(eh.build_request("pressure", 2, [2019, 2020])[1]["day"]) == 28  # only days that exist in every year
    _, rain = eh.build_request("precip", 10, [2019])
    assert rain["area"] == eh.live.PRECIP_AREA and len(rain["time"]) == 24 and len(rain["day"]) == 31
    with pytest.raises(ValueError):
        eh.build_request("nope", 10, [2019])


def test_whole_history_is_six_requests_inside_the_field_limit():
    jobs = eh.plan(eh.DEFAULT_YEARS, eh.DEFAULT_MONTHS, len(eh.DEFAULT_YEARS), Path("/nonexistent"))
    assert len(jobs) == 6 and {k for k, _, _ in jobs} == set(eh.KINDS)
    assert max(eh.field_count(eh.build_request(k, m, ys)[1]) for k, m, ys in jobs) < 120_000  # CDS per-request limit


def test_firms_chunks_cover_each_month_in_5_day_pieces_and_sources_start_in_their_year(tmp_path):
    import fetch_firms_history as fh

    chunks = fh.month_chunks(2019, [10, 11])
    assert chunks[0] == ("2019-10-01", 5) and chunks[-1] == ("2019-11-26", 5)
    assert sum(d for _, d in chunks) == 61 and len(chunks) == 13
    jobs = fh.missing_jobs([2016, 2018], tmp_path)
    assert ("VIIRS_NOAA20_SP", 2016) not in jobs and ("VIIRS_NOAA20_SP", 2018) in jobs and ("VIIRS_SNPP_SP", 2016) in jobs
    fh.out_path("VIIRS_SNPP_SP", 2016, tmp_path).write_text("x")
    assert ("VIIRS_SNPP_SP", 2016) not in fh.missing_jobs([2016], tmp_path)


def test_hist_stations_and_coordinate_attach(tmp_path):
    import build_station_table as bt

    col = bt.PM25
    for year, pm in ((2018, [None, None]), (2019, [10.0, 20.0])):
        pd.DataFrame({"Station ID": ["s1", "s1"], "State": ["Delhi"] * 2, "City": ["Delhi"] * 2,
                      "Station Name": ["Bawana, Delhi - DPCC"] * 2, col: pm}).to_parquet(tmp_path / f"cpcb-air-quality-{year}.parquet")
    st = bt.hist_stations(tmp_path)
    row = st.iloc[0]
    assert len(st) == 1 and row["first_year"] == 2019 and row["years_with_pm25"] == 1 and row["pm25_records"] == 2
    locs = pd.DataFrame({"id": [7, 8], "name": ["bawana, delhi - dpcc ", "Bawana, Delhi - DPCC"], "provider": ["CPCB", "AirGradient"],
                         "lat": [28.77, 1.0], "lon": [77.05, 1.0]})
    got = bt.attach_coordinates(st, locs).iloc[0]
    assert got["latitude"] == 28.77 and got["openaq_id"] == 7  # government provider, name matched ignoring case and spaces


def _quarter_hours(station, start_utc, values):
    ts = pd.date_range(start_utc, periods=len(values), freq="15min")
    pm25 = pd.Series(values, dtype=float)
    return pd.DataFrame({"Station ID": station, "Timestamp": ts, "PM2.5 (µg/m³)": pm25, "PM10 (µg/m³)": pm25 * 2})


def test_silver_hourly_means_ist_and_completeness_rule():
    import build_silver as bs

    vals = [10, 20, 30, 40] + [50, 60, None, None]  # hour 1 complete, hour 2 has only 2 of 4 readings
    out = bs.hourly_means(_quarter_hours("s1", "2019-11-01 00:30", vals))  # 00:30 UTC is 06:00 IST
    assert out.loc[0, "hour_start_ist"] == pd.Timestamp("2019-11-01 06:00")
    assert out.loc[0, "pm25"] == 25 and out.loc[0, "pm10"] == 50
    assert pd.isna(out.loc[1, "pm25"])  # fewer than MIN_READINGS readings


def test_silver_clean_drops_out_of_range_and_stuck_runs():
    import build_silver as bs

    hours = pd.date_range("2019-11-01", periods=12, freq="h")
    pm = [30, 31, 0, 2000, 55, 55, 55, 55, 55, 55, 60, 61]  # zero and 2000 out of range; six identical hours stuck
    hourly = pd.DataFrame({"station_id": "s1", "hour_start_ist": hours, "pm25": pm, "pm10": pm})
    clean, st = bs.clean_hourly(hourly)
    assert st["hours_raw"] == 12 and st["removed_range"] == 2 and st["removed_flatline"] == 6
    assert clean["pm25"].notna().sum() == 4 and st["hours_clean"] == 4 and st["stations"] == 1


def test_national_audit_flags_a_stuck_station_and_leaves_isolated_ones_unchecked():
    import national_audit as na

    rng = np.random.default_rng(1)
    hours = pd.date_range("2019-01-01", "2019-12-31 23:00", freq="h")
    base = 60 + 30 * np.sin(np.arange(len(hours)) / 40) + rng.normal(0, 3, len(hours))
    cols = {f"s{i}": base * (1 + 0.05 * i) + rng.normal(0, 2, len(hours)) for i in range(4)}
    pm = pd.DataFrame(cols, index=hours)
    pm.iloc[1000:1050, pm.columns.get_loc("s0")] = 77.0  # a stuck sensor: 50 identical hours
    pm["far"] = base  # reports fine but sits 500 km from everyone
    coords = pd.DataFrame({"station": ["s0", "s1", "s2", "s3", "far"], "latitude": [28.60, 28.61, 28.62, 28.63, 33.0],
                           "longitude": [77.20, 77.21, 77.22, 77.23, 77.2]})
    h = na.audit_year(pm, coords).set_index("station")
    assert h.loc["s0", "flatline_hours"] >= 50 and h.loc["s0", "level"] == "flag"
    assert h.loc["far", "level"] == "none"  # no neighbour within 25 km: cannot be checked, must not read as ok
    h["year"] = 2019
    h = h.reset_index().rename(columns={"station": "station_id"})
    h["failing_share"] = na.failing_share(h)
    h["drifting"] = False
    master = pd.DataFrame({"cpcb_id": ["s0", "s1", "s2", "s3", "far"], "city": ["Delhi"] * 4 + ["Leh"], "state": ["Delhi"] * 4 + ["Ladakh"]})
    league = na.city_league(h, master).set_index("city")
    assert league.loc["Delhi", "flagged"] >= 1 and league.loc["Delhi", "stations_audited"] == 4
    assert league.loc["Leh", "stations_audited"] == 0 and np.isnan(league.loc["Leh", "failing_share"])


def test_gold_fire_delhi_and_completeness(tmp_path):
    import build_gold as bg

    pd.DataFrame({"source": "VIIRS_SNPP_SP", "acq_date": ["2019-10-01", "2019-10-01", "2019-10-02"], "frp": [10.0, 5.5, 2.0]}).to_csv(tmp_path / "firms_VIIRS_SNPP_SP_2019.csv", index=False)
    fd = bg.fire_daily(tmp_path).set_index("date")
    assert fd.loc["2019-10-01", "detections"] == 2 and fd.loc["2019-10-01", "frp_sum"] == 15.5

    hours = pd.date_range("2019-10-01", periods=48, freq="h")
    rows = [{"station_id": s, "hour_start_ist": h, "pm25": v} for s, v, n in (("d1", 100.0, 48), ("d2", 50.0, 10), ("x1", 9.0, 48)) for h in hours[:n]]
    silver = pd.DataFrame(rows)
    dd = bg.delhi_daily(silver, {"d1", "d2"})
    assert list(dd["pm25"]) == [100.0, 100.0] and list(dd["stations"]) == [1, 1]  # d2 has <18 hours a day: not counted
    comp = bg.completeness(silver, {"d1": "Delhi", "d2": "Delhi", "x1": "Goa"}).set_index("state")
    assert comp.loc["Goa", "stations"] == 1 and comp.loc["Goa", "completeness"] == round(48 / (31 * 24), 3)


def test_cpcb_history_skips_finished_years_only(tmp_path):
    import fetch_cpcb_history as fc

    (tmp_path / fc.asset_name(2016)).write_text("x")
    (tmp_path / (fc.asset_name(2017) + ".part")).write_text("x")  # interrupted: must be fetched again
    assert fc.missing_years([2015, 2016, 2017], tmp_path) == [2015, 2017]


def test_plan_skips_existing_files_and_can_split_years(tmp_path):
    eh.out_path("single", 10, [2024, 2025], tmp_path).write_text("x")
    jobs = eh.plan([2024, 2025], [10], 2, tmp_path)
    assert ("single", 10, [2024, 2025]) not in jobs and len(jobs) == 2
    split = eh.plan([2022, 2023, 2024, 2025], [10], 2, tmp_path)
    assert len(split) == 5 and split[0][2] == [2024, 2025] and split[-1][2] == [2022, 2023]  # newest years first

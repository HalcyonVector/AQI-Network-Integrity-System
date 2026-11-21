import sys
from pathlib import Path

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
    ds, single = eh.build_request("single", 2019, 11)
    assert ds == eh.live.DATASET and len(single["day"]) == 30 and single["area"] == eh.live.AREA
    assert single["variable"] == eh.live.VARIABLES and single["time"] == eh.live.HOURS
    ds, pr = eh.build_request("pressure", 2020, 2)
    assert ds == eh.live.PRESSURE_DATASET and len(pr["day"]) == 29 and pr["pressure_level"] == [eh.live.PRESSURE_LEVEL]
    _, rain = eh.build_request("precip", 2019, 10)
    assert rain["area"] == eh.live.PRECIP_AREA and len(rain["time"]) == 24 and len(rain["day"]) == 31
    with pytest.raises(ValueError):
        eh.build_request("nope", 2019, 10)


def test_firms_chunks_cover_each_month_in_5_day_pieces_and_sources_start_in_their_year(tmp_path):
    import fetch_firms_history as fh

    chunks = fh.month_chunks(2019, [10, 11])
    assert chunks[0] == ("2019-10-01", 5) and chunks[-1] == ("2019-11-26", 5)
    assert sum(d for _, d in chunks) == 61 and len(chunks) == 13
    jobs = fh.missing_jobs([2016, 2018], tmp_path)
    assert ("VIIRS_NOAA20_SP", 2016) not in jobs and ("VIIRS_NOAA20_SP", 2018) in jobs and ("VIIRS_SNPP_SP", 2016) in jobs
    fh.out_path("VIIRS_SNPP_SP", 2016, tmp_path).write_text("x")
    assert ("VIIRS_SNPP_SP", 2016) not in fh.missing_jobs([2016], tmp_path)


def test_cpcb_history_skips_finished_years_only(tmp_path):
    import fetch_cpcb_history as fc

    (tmp_path / fc.asset_name(2016)).write_text("x")
    (tmp_path / (fc.asset_name(2017) + ".part")).write_text("x")  # interrupted: must be fetched again
    assert fc.missing_years([2015, 2016, 2017], tmp_path) == [2015, 2017]


def test_missing_jobs_skip_existing_and_go_newest_first(tmp_path):
    eh.out_path("single", 2025, 10, tmp_path).write_text("x")
    jobs = eh.missing_jobs([2024, 2025], [10], tmp_path)
    assert ("single", 2025, 10) not in jobs and len(jobs) == 5
    assert jobs[0][1] == 2025 and jobs[-1][1] == 2024

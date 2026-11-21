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


def test_missing_jobs_skip_existing_and_go_newest_first(tmp_path):
    eh.out_path("single", 2025, 10, tmp_path).write_text("x")
    jobs = eh.missing_jobs([2024, 2025], [10], tmp_path)
    assert ("single", 2025, 10) not in jobs and len(jobs) == 5
    assert jobs[0][1] == 2025 and jobs[-1][1] == 2024

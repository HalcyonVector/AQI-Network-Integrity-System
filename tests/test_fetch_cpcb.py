import logging
from pathlib import Path

import pandas as pd
import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_cpcb  # noqa: E402


@pytest.fixture
def logger():
    log = logging.getLogger("test_fetch_cpcb")
    log.addHandler(logging.NullHandler())
    return log


def make_record(**overrides):
    base = {
        "country": "India",
        "state": "Delhi",
        "city": "Delhi",
        "station": "ITO, Delhi - CPCB",
        "last_update": "17-09-2026 01:00:00",
        "latitude": "28.63",
        "longitude": "77.24",
        "pollutant_id": "PM2.5",
        "min_value": "50",
        "max_value": "90",
        "avg_value": "70",
    }
    base.update(overrides)
    return base


def test_filters_to_ncr_cities_only(logger):
    records = [
        make_record(city="Delhi", station="A"),
        make_record(city="Mumbai", station="B"),
        make_record(city="Gaya", station="C"),
    ]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert set(out["city"]) == {"Delhi"}


def test_filters_to_target_pollutants_only(logger):
    records = [
        make_record(pollutant_id="PM2.5"),
        make_record(pollutant_id="NH3"),  # not in TARGET_POLLUTANTS
    ]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert "PM2.5" in out.columns
    assert "NH3" not in out.columns


def test_pivots_multiple_pollutants_into_one_row_per_station(logger):
    records = [
        make_record(station="ITO", pollutant_id="PM2.5", avg_value="70"),
        make_record(station="ITO", pollutant_id="PM10", avg_value="120"),
        make_record(station="ITO", pollutant_id="NO2", avg_value="30"),
    ]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["PM2.5"] == 70.0
    assert row["PM10"] == 120.0
    assert row["NO2"] == 30.0


def test_missing_pollutant_columns_are_present_as_na(logger):
    records = [make_record(station="ITO", pollutant_id="PM2.5", avg_value="70")]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    for pollutant in fetch_cpcb.TARGET_POLLUTANTS:
        assert pollutant in out.columns
    assert pd.isna(out.iloc[0]["OZONE"])


def test_empty_records_returns_empty_dataframe(logger):
    out = fetch_cpcb.filter_and_reshape([], logger)
    assert out.empty


def test_no_ncr_matches_returns_empty_dataframe(logger):
    records = [make_record(city="Mumbai")]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert out.empty


def test_missing_expected_column_raises(logger):
    records = [{"city": "Delhi", "station": "ITO"}]  # no pollutant_id/avg_value
    with pytest.raises(ValueError, match="missing columns"):
        fetch_cpcb.filter_and_reshape(records, logger)


def test_blank_station_name_is_dropped(logger):
    records = [
        make_record(station="ITO", pollutant_id="PM2.5"),
        make_record(station="", pollutant_id="PM2.5"),
        make_record(station=None, pollutant_id="PM2.5"),
    ]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert len(out) == 1
    assert out.iloc[0]["station"] == "ITO"


def test_non_numeric_pollutant_value_becomes_nan_not_crash(logger):
    records = [make_record(station="ITO", pollutant_id="PM2.5", avg_value="NA")]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert pd.isna(out.iloc[0]["PM2.5"])


def test_non_numeric_coordinates_become_nan_not_crash(logger):
    records = [make_record(station="ITO", latitude="not-a-number", longitude="also-bad")]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert pd.isna(out.iloc[0]["latitude"])
    assert pd.isna(out.iloc[0]["longitude"])


def test_duplicate_station_pollutant_reading_keeps_one_row(logger):
    records = [
        make_record(station="ITO", pollutant_id="PM2.5", avg_value="70"),
        make_record(station="ITO", pollutant_id="PM2.5", avg_value="999"),
    ]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert len(out) == 1


def test_multiple_stations_across_cities_do_not_cross_multiply(logger):
    # Regression test: an earlier pivot_table(dropna=False) implementation
    # generated the full cartesian product of every (state, city) pair seen
    # anywhere in the data crossed with every station, turning 2 real
    # stations into dozens of bogus rows with fabricated city/state combos.
    records = [
        make_record(state="Delhi", city="Delhi", station="ITO", pollutant_id="PM2.5", avg_value="55"),
        make_record(state="Haryana", city="Gurugram", station="Sector-51", pollutant_id="PM2.5", avg_value="60"),
    ]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert len(out) == 2
    assert set(zip(out["city"], out["station"])) == {("Delhi", "ITO"), ("Gurugram", "Sector-51")}


def test_station_with_only_nan_reading_is_not_dropped(logger):
    records = [make_record(station="ITO", pollutant_id="PM2.5", avg_value="not-a-number")]
    out = fetch_cpcb.filter_and_reshape(records, logger)
    assert len(out) == 1
    assert out.iloc[0]["station"] == "ITO"
    assert pd.isna(out.iloc[0]["PM2.5"])


def test_append_to_csv_rejects_schema_mismatch(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_cpcb, "RAW_DATA_DIR", tmp_path)
    df1 = pd.DataFrame({"a": [1], "b": [2]})
    df2 = pd.DataFrame({"a": [1], "c": [2]})

    fetch_cpcb.append_to_csv(df1, logger)
    with pytest.raises(ValueError, match="Column mismatch"):
        fetch_cpcb.append_to_csv(df2, logger)


def test_append_to_csv_appends_without_duplicating_header(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_cpcb, "RAW_DATA_DIR", tmp_path)
    df = pd.DataFrame({"a": [1], "b": [2]})

    out_path = fetch_cpcb.append_to_csv(df, logger)
    fetch_cpcb.append_to_csv(df, logger)

    lines = out_path.read_text().strip().splitlines()
    assert lines[0] == "a,b"
    assert len(lines) == 3  # header + 2 data rows

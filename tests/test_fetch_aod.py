import logging
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_aod  # noqa: E402


@pytest.fixture
def logger():
    log = logging.getLogger("test_fetch_aod")
    log.addHandler(logging.NullHandler())
    return log


def test_parse_sampled_result_extracts_expected_fields():
    result = {
        "features": [
            {
                "properties": {
                    "station": "ITO, Delhi - CPCB",
                    "city": "Delhi",
                    "Optical_Depth_047": 0.35,
                    "Optical_Depth_055": 0.31,
                }
            }
        ]
    }
    out = fetch_aod.parse_sampled_result(result, "2026-09-16")
    assert len(out) == 1
    assert out.iloc[0]["station"] == "ITO, Delhi - CPCB"
    assert out.iloc[0]["date"] == "2026-09-16"
    assert out.iloc[0]["aod_047"] == 0.35


def test_parse_sampled_result_feature_without_bands_is_nan():
    result = {"features": [{"properties": {"station": "Foo", "city": "Delhi"}}]}
    out = fetch_aod.parse_sampled_result(result, "2026-09-16")
    assert len(out) == 1
    assert pd.isna(out.iloc[0]["aod_047"])
    assert pd.isna(out.iloc[0]["aod_055"])


def test_complete_with_all_stations_adds_nan_rows_for_dropped_points():
    # Regression: live run returned 1 row for 62 stations because Earth Engine's
    # sampleRegions drops points with masked pixels entirely instead of returning
    # nulls. Every input station must come back, NaN where the sensor was blind.
    stations = pd.DataFrame(
        {"station": ["A", "B", "C"], "city": ["Delhi", "Delhi", "Noida"], "latitude": [1, 2, 3], "longitude": [4, 5, 6]}
    )
    parsed = fetch_aod.parse_sampled_result(
        {"features": [{"properties": {"station": "B", "city": "Delhi", "Optical_Depth_047": 0.4, "Optical_Depth_055": 0.35}}]},
        "2026-09-24",
    )
    out = fetch_aod.complete_with_all_stations(parsed, stations, "2026-09-24")
    assert list(out["station"]) == ["A", "B", "C"]
    assert (out["date"] == "2026-09-24").all()
    assert out.set_index("station").loc["B", "aod_047"] == 0.4
    assert pd.isna(out.set_index("station").loc["A", "aod_047"])
    assert pd.isna(out.set_index("station").loc["C", "aod_055"])


def test_parse_sampled_result_empty_features_returns_empty_df():
    out = fetch_aod.parse_sampled_result({"features": []}, "2026-09-16")
    assert out.empty
    assert list(out.columns) == ["date", "station", "city", "aod_047", "aod_055"]


def test_load_station_points_uses_most_recent_cpcb_file(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_aod, "RAW_DATA_DIR", tmp_path)

    older = pd.DataFrame(
        {"station": ["Old Station"], "city": ["Delhi"], "latitude": [28.6], "longitude": [77.2]}
    )
    older.to_csv(tmp_path / "cpcb_ncr_2026-09-15.csv", index=False)

    newer = pd.DataFrame(
        {"station": ["New Station"], "city": ["Delhi"], "latitude": [28.7], "longitude": [77.3]}
    )
    newer.to_csv(tmp_path / "cpcb_ncr_2026-09-16.csv", index=False)

    out = fetch_aod.load_station_points(logger)
    assert list(out["station"]) == ["New Station"]


def test_load_station_points_drops_missing_coordinates(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_aod, "RAW_DATA_DIR", tmp_path)
    df = pd.DataFrame(
        {
            "station": ["Good", "Bad"],
            "city": ["Delhi", "Delhi"],
            "latitude": [28.6, None],
            "longitude": [77.2, None],
        }
    )
    df.to_csv(tmp_path / "cpcb_ncr_2026-09-16.csv", index=False)

    out = fetch_aod.load_station_points(logger)
    assert list(out["station"]) == ["Good"]


def test_load_station_points_raises_when_no_cpcb_data(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_aod, "RAW_DATA_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="fetch_cpcb.py"):
        fetch_aod.load_station_points(logger)


def test_write_date_file_is_idempotent_and_named_by_observation_date(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_aod, "RAW_DATA_DIR", tmp_path)
    df = pd.DataFrame({"date": ["2026-10-06", "2026-10-06"], "station": ["A", "B"], "aod_047": [0.5, 0.6]})

    path1 = fetch_aod.write_date_file(df, logger)
    path2 = fetch_aod.write_date_file(df, logger)  # re-run the same date

    assert path1 == path2 == tmp_path / "aod_ncr_2026-10-06.csv"
    assert len(pd.read_csv(path1)) == 2  # overwritten, not appended


def test_write_date_file_rejects_mixed_observation_dates(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_aod, "RAW_DATA_DIR", tmp_path)
    df = pd.DataFrame({"date": ["2026-10-05", "2026-10-06"], "station": ["A", "B"]})
    with pytest.raises(ValueError, match="exactly one observation date"):
        fetch_aod.write_date_file(df, logger)

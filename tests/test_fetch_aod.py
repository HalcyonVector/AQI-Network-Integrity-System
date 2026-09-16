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


def test_parse_sampled_result_keeps_masked_pixel_as_nan():
    # A station under cloud cover / bad QA gets no Optical_Depth_* property back
    # from Earth Engine at all (masked pixels are dropped from properties, not
    # zeroed) -- must show up as NaN, not silently vanish or become 0.
    result = {"features": [{"properties": {"station": "Foo", "city": "Delhi"}}]}
    out = fetch_aod.parse_sampled_result(result, "2026-09-16")
    assert len(out) == 1
    assert pd.isna(out.iloc[0]["aod_047"])
    assert pd.isna(out.iloc[0]["aod_055"])


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


def test_append_to_csv_rejects_schema_mismatch(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_aod, "RAW_DATA_DIR", tmp_path)
    df1 = pd.DataFrame({"date": ["2026-09-16"], "station": ["A"]})
    df2 = pd.DataFrame({"date": ["2026-09-16"], "aod_047": [0.3]})

    fetch_aod.append_to_csv(df1, logger)
    with pytest.raises(ValueError, match="Column mismatch"):
        fetch_aod.append_to_csv(df2, logger)

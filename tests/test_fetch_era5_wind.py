import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_era5_wind  # noqa: E402


@pytest.fixture
def logger():
    log = logging.getLogger("test_fetch_era5_wind")
    log.addHandler(logging.NullHandler())
    return log


def make_dataset(u10, v10, expver=None):
    lat = [28.5, 29.0]
    lon = [77.0, 77.5]
    time = pd.to_datetime(["2026-09-10T00:00", "2026-09-10T03:00"])

    if expver is None:
        data = {
            "u10": (["time", "latitude", "longitude"], np.full((2, 2, 2), u10)),
            "v10": (["time", "latitude", "longitude"], np.full((2, 2, 2), v10)),
        }
        coords = {"time": time, "latitude": lat, "longitude": lon}
    else:
        data = {
            "u10": (["time", "expver", "latitude", "longitude"], np.full((2, len(expver), 2, 2), u10)),
            "v10": (["time", "expver", "latitude", "longitude"], np.full((2, len(expver), 2, 2), v10)),
        }
        coords = {"time": time, "expver": expver, "latitude": lat, "longitude": lon}

    return xr.Dataset(data, coords=coords)


def test_dataset_to_dataframe_computes_speed_and_direction(logger):
    # Wind blowing due south: u=0, v=-5 m/s -- i.e. FROM the north.
    ds = make_dataset(u10=0.0, v10=-5.0)
    df = fetch_era5_wind.dataset_to_dataframe(ds, logger)

    assert not df.empty
    assert (df["wind_speed_ms"].round(1) == 5.0).all()
    assert (df["wind_from_deg"].round(0) == 0.0).all()  # from the north


def test_dataset_to_dataframe_wind_from_east(logger):
    # u=-5 (blowing westward), v=0 -- wind is FROM the east (90 deg).
    ds = make_dataset(u10=-5.0, v10=0.0)
    df = fetch_era5_wind.dataset_to_dataframe(ds, logger)
    assert (df["wind_from_deg"].round(0) == 90.0).all()


def test_dataset_to_dataframe_collapses_expver_dimension(logger):
    # ERA5T responses can carry an expver dimension (preliminary vs final), where
    # only ONE expver slice has real data at a given point/time (the other is NaN)
    # -- must collapse to that single real value in a flat per-point result, not
    # leave duplicate rows per expver or average a real value against a NaN.
    ds = make_dataset(u10=3.0, v10=4.0, expver=["0001", "0005"])
    ds["u10"].values[:, 1, :, :] = np.nan  # expver=0005 (index 1) has no data
    ds["v10"].values[:, 1, :, :] = np.nan

    df = fetch_era5_wind.dataset_to_dataframe(ds, logger)
    assert "expver" not in df.columns
    # 2 times x 2 lat x 2 lon = 8 rows, NOT doubled by the 2 expver values
    assert len(df) == 8
    assert (df["u10_ms"] == 3.0).all()
    assert (df["v10_ms"] == 4.0).all()


def test_dataset_to_dataframe_keeps_only_expected_columns(logger):
    ds = make_dataset(u10=1.0, v10=1.0)
    df = fetch_era5_wind.dataset_to_dataframe(ds, logger)
    expected = {"time", "latitude", "longitude", "u10_ms", "v10_ms", "wind_speed_ms", "wind_from_deg"}
    assert set(df.columns) == expected


def test_append_to_csv_rejects_schema_mismatch(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_era5_wind, "RAW_DATA_DIR", tmp_path)
    df1 = pd.DataFrame({"latitude": [28.5], "u10_ms": [1.0]})
    df2 = pd.DataFrame({"latitude": [28.5], "wind_speed_ms": [1.0]})

    fetch_era5_wind.append_to_csv(df1, logger)
    with pytest.raises(ValueError, match="Column mismatch"):
        fetch_era5_wind.append_to_csv(df2, logger)

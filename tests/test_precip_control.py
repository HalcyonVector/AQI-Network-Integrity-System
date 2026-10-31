import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_stubble_transport as ast_  # noqa: E402
import fetch_era5_wind as fw  # noqa: E402


@pytest.fixture
def logger():
    log = logging.getLogger("test_precip")
    log.addHandler(logging.NullHandler())
    return log


# ---------------------------------------------------------------- parsing the ERA5 hourly precipitation


def _tp_ds(values_by_hour, expver=False):
    times = pd.date_range("2026-09-27 00:00", periods=len(values_by_hour), freq="h")
    arr = np.zeros((len(values_by_hour), 2, 2))
    for i, v in enumerate(values_by_hour):
        arr[i] = v  # metres of water, the same in every cell...
    arr[:, 0, 0] *= 3  # ...except one cell that gets three times as much
    coords = {"valid_time": times, "latitude": [28.5, 28.75], "longitude": [77.0, 77.25]}
    ds = xr.Dataset({"tp": (["valid_time", "latitude", "longitude"], arr)}, coords=coords)
    if expver:
        ds = ds.assign_coords(expver=("valid_time", ["0005"] * len(times)))
    return ds


def test_precip_parsing_converts_metres_to_mm_and_reports_area_mean_and_max(logger):
    out = fw.precip_dataset_to_hourly(_tp_ds([0.001, 0.0, 0.002]), logger)
    assert list(out.columns) == ["time", "precip_mm_delhi_mean", "precip_mm_delhi_max"]
    first = out.iloc[0]
    assert first["precip_mm_delhi_max"] == pytest.approx(3.0)  # 0.001 m x 3 = 3 mm in the wettest cell
    assert first["precip_mm_delhi_mean"] == pytest.approx((3 + 1 + 1 + 1) / 4)
    assert out.iloc[1]["precip_mm_delhi_max"] == 0.0 and len(out) == 3


def test_precip_parsing_clips_float_noise_negatives_to_zero(logger):
    out = fw.precip_dataset_to_hourly(_tp_ds([-1e-9, 0.0]), logger)
    assert (out["precip_mm_delhi_mean"] >= 0).all() and (out["precip_mm_delhi_max"] >= 0).all()


def test_precip_parsing_rejects_an_unexpected_response(logger):
    bad = xr.Dataset({"other": (["valid_time"], [1.0])}, coords={"valid_time": pd.date_range("2026-09-27", periods=1, freq="h")})
    with pytest.raises(ValueError, match="unexpected precipitation response"):
        fw.precip_dataset_to_hourly(bad, logger)


def test_a_failed_precip_fetch_never_raises_so_it_cannot_block_the_wind_data(logger, tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "RAW_DATA_DIR", tmp_path)  # an empty folder, never the real data on disk

    class Boom:
        def retrieve(self, *a, **k):
            raise RuntimeError("CDS down")

    from datetime import datetime

    assert fw.fetch_and_write_precip(Boom(), datetime(2026, 9, 27), logger) is None


# ---------------------------------------------------------------- daily rain, washout, IST day bucketing


def _hourly(by_day_mm, start_utc="2026-09-26 19:00"):
    """Hourly area-mean precipitation. IST day D owns the hours ending D-1 19:00 .. D 18:00 UTC."""
    rows = []
    t = pd.Timestamp(start_utc)
    for mm in by_day_mm:
        for _ in range(24):
            rows.append((t, mm / 24.0, mm / 24.0))
            t += pd.Timedelta(hours=1)
    return pd.DataFrame(rows, columns=["time", "precip_mm_delhi_mean", "precip_mm_delhi_max"])


def test_daily_precip_sums_24_hours_into_the_right_ist_day():
    out = ast_.delhi_daily_precip(_hourly([0.0, 6.0, 0.0])).set_index("date")
    # the first stamp (09-26 19:00 UTC = 09-27 00:30 IST) opens IST day 09-27
    assert list(out.index) == ["2026-09-27", "2026-09-28", "2026-09-29"]
    assert out.loc["2026-09-28", "precip_delhi_mm"] == pytest.approx(6.0)
    assert out.loc["2026-09-27", "precip_delhi_mm"] == pytest.approx(0.0)


def test_daily_precip_requires_all_24_hours_of_the_day():
    h = _hourly([0.0, 6.0])
    h = h.drop(index=h.index[30:35])  # five hours missing from the second day
    out = ast_.delhi_daily_precip(h)
    assert list(out["date"]) == ["2026-09-27"]


def test_washout_covers_the_wet_day_and_the_following_days_but_not_before():
    out = ast_.delhi_daily_precip(_hourly([0, 0, 3.0, 0, 0, 0, 0])).set_index("date")
    flags = out["washout"].tolist()
    assert flags == [False, False, True, True, True, False, False]  # wet day + 2 days after


def test_a_light_shower_below_the_threshold_is_not_a_rain_day():
    out = ast_.delhi_daily_precip(_hourly([0.0, ast_.RAIN_DAY_MM - 0.1, 0.0]))
    assert not out["washout"].any()


def test_a_missing_day_does_not_extend_the_washout_window():
    h = _hourly([3.0, 0.0, 0.0, 0.0, 0.0])
    keep = ~((h["time"] >= pd.Timestamp("2026-09-28 19:00")) & (h["time"] < pd.Timestamp("2026-09-29 19:00")))  # drop IST day 09-29
    out = ast_.delhi_daily_precip(h[keep]).set_index("date")
    assert "2026-09-29" not in out.index
    assert out.loc["2026-09-30", "washout"] == False  # noqa: E712 - 3 days after the rain: outside the window


def test_daily_precip_degrades_gracefully_without_data():
    assert ast_.delhi_daily_precip(pd.DataFrame()).empty
    assert list(ast_.delhi_daily_precip(pd.DataFrame({"time": []})).columns) == ["date", "precip_delhi_mm", "washout"]


# ---------------------------------------------------------------- the dry-days check


def _daily_frame(n=14, washout_first=3, seed=0, exposure_drives_pm=True):
    rng = np.random.default_rng(seed)
    exposure = rng.uniform(1, 10, n)
    pm = 40 + 4 * exposure + rng.normal(scale=2, size=n) if exposure_drives_pm else rng.normal(60, 10, n)
    washout = np.array([True] * washout_first + [False] * (n - washout_first))
    pm = np.where(washout, pm * 0.4, pm)  # rain scrubbed the air on wet days
    return pd.DataFrame({"date": [f"2026-10-{i + 1:02d}" for i in range(n)], "pm25_delhi": pm, "e": exposure, "washout": washout})


def test_dry_days_association_drops_washout_days_and_reports_counts():
    r = ast_.dry_days_association(_daily_frame(), "e")
    assert r["n_wet_days"] == 3 and r["n_dry_days"] == 11
    assert r["spearman"] > 0.7  # a genuine exposure effect survives once rain days are removed


def test_dry_days_association_collapses_when_rain_was_the_whole_story():
    # PM is driven ONLY by rain; exposure just happens to be low on the rainy days.
    rng = np.random.default_rng(3)
    n = 20
    washout = np.array([True] * 7 + [False] * (n - 7))  # enough wet days that the regime split alone gives a strong correlation
    exposure = np.where(washout, rng.uniform(0, 2, n), rng.uniform(4, 10, n))
    pm = np.where(washout, 15.0, 70.0) + rng.normal(scale=3, size=n)
    d = pd.DataFrame({"pm25_delhi": pm, "e": exposure, "washout": washout})
    full, _ = ast_.permutation_spearman(d["e"].to_numpy(), d["pm25_delhi"].to_numpy(), n_perm=300)
    dry = ast_.dry_days_association(d, "e")
    assert full > 0.6 and abs(dry["spearman"]) < 0.5  # the apparent link was the rain


def test_dry_days_association_says_so_when_there_are_too_few_dry_days():
    d = _daily_frame(n=12, washout_first=8)
    r = ast_.dry_days_association(d, "e")
    assert r["spearman"] is None and r["note"] == "too few dry days"


def test_dry_days_association_is_none_without_rain_data():
    assert ast_.dry_days_association(_daily_frame().drop(columns="washout"), "e") is None


def test_a_complete_precip_file_is_not_requested_again(tmp_path, monkeypatch, logger):
    from datetime import datetime

    monkeypatch.setattr(fw, "RAW_DATA_DIR", tmp_path)
    day = datetime(2026, 9, 27)
    pd.DataFrame({"time": pd.date_range("2026-09-27", periods=24, freq="h"), "precip_mm_delhi_mean": 0.0, "precip_mm_delhi_max": 0.0}).to_csv(
        tmp_path / "era5_precip_2026-09-27.csv", index=False)
    assert fw.precip_file_complete(day)

    class MustNotBeCalled:
        def retrieve(self, *a, **k):
            raise AssertionError("asked CDS for data we already have")

    assert fw.fetch_and_write_precip(MustNotBeCalled(), day, logger) == tmp_path / "era5_precip_2026-09-27.csv"


def test_a_partial_or_missing_precip_file_is_requested(tmp_path, monkeypatch, logger):
    from datetime import datetime

    monkeypatch.setattr(fw, "RAW_DATA_DIR", tmp_path)
    assert not fw.precip_file_complete(datetime(2026, 9, 27))  # nothing there
    pd.DataFrame({"time": pd.date_range("2026-09-27", periods=10, freq="h"), "precip_mm_delhi_mean": 0.0, "precip_mm_delhi_max": 0.0}).to_csv(
        tmp_path / "era5_precip_2026-09-27.csv", index=False)
    assert not fw.precip_file_complete(datetime(2026, 9, 27))  # only 10 of 24 hours


def test_wind_file_complete_only_when_every_timestamp_is_present(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(fw, "RAW_DATA_DIR", tmp_path)
    day = datetime(2026, 10, 20)
    assert not fw.wind_file_complete(day)  # nothing there
    path = tmp_path / "era5_wind_2026-10-20.csv"
    pd.DataFrame({"time": pd.date_range("2026-10-20", periods=5, freq="3h").repeat(2)}).to_csv(path, index=False)
    assert not fw.wind_file_complete(day)  # partial day: 5 of 8 timestamps, must be re-requested
    pd.DataFrame({"time": pd.date_range("2026-10-20", periods=8, freq="3h").repeat(2)}).to_csv(path, index=False)
    assert fw.wind_file_complete(day)
    path.write_text("garbage without the expected column", encoding="utf-8")
    assert not fw.wind_file_complete(day)  # unreadable file is re-requested, not trusted

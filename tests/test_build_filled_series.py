import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_neighbor_consistency as nc  # noqa: E402
import build_filled_series as bf  # noqa: E402
import qc  # noqa: E402


def make_network(n_hours=120, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-10-05", periods=n_hours, freq="h")
    common = np.log(60) + 0.4 * np.sin(np.arange(n_hours) / 7)
    cols = {f"S{i}": np.exp(common + 0.2 * i + rng.normal(scale=0.04, size=n_hours)) for i in range(5)}
    coords = pd.DataFrame({"station": list(cols), "latitude": 28.5 + 0.02 * np.arange(5), "longitude": 77.1})
    return pd.DataFrame(cols, index=idx), coords


def run(raw, coords, **kw):
    clean = qc.mask_flatlines(raw)
    nb = nc.build_neighbors(coords, list(clean.columns))
    z = np.zeros(clean.shape)
    pred = nc.predict_all(np.log(clean.to_numpy(float)), nb, z, z)["knn_median"]
    return bf.fill_gaps(raw, clean, pred, **kw)


def rows(out, station):
    return out[out["station"] == station].reset_index(drop=True)


def test_interior_gap_runs_ignores_leading_and_trailing_blanks():
    v = np.array([np.nan, np.nan, 5, 6, np.nan, np.nan, 7, 8, np.nan], float)
    assert bf.interior_gap_runs(v) == [(4, 2)]


def test_short_interior_gap_is_filled_close_to_the_truth_and_labelled():
    raw, coords = make_network()
    truth = raw.loc[raw.index[40:46], "S2"].copy()
    raw.loc[raw.index[40:46], "S2"] = np.nan
    out = rows(run(raw, coords), "S2")
    gap = out.iloc[40:46]
    assert (gap["source"] == "neighbour_fill").all()
    assert gap["expected_abs_error_pct"].notna().all()
    assert np.mean(np.abs(gap["pm25_final"].to_numpy() - truth.to_numpy()) / truth.to_numpy()) < 0.15
    assert gap["pm25_measured"].isna().all()


def test_observed_values_are_never_modified():
    raw, coords = make_network()
    raw.loc[raw.index[40:46], "S2"] = np.nan
    out = rows(run(raw, coords), "S2")
    obs = out[out["source"] == "observed"]
    assert np.allclose(obs["pm25_final"], obs["pm25_measured"].round(2))
    assert obs["expected_abs_error_pct"].isna().all()


def test_gap_longer_than_the_validated_limit_is_left_unfilled():
    raw, coords = make_network()
    raw.loc[raw.index[30:60], "S2"] = np.nan  # 30 h > 24 h limit
    out = rows(run(raw, coords), "S2")
    assert (out.iloc[30:60]["source"] == "unfilled").all()
    assert out.iloc[30:60]["pm25_final"].isna().all()


def test_the_limit_is_configurable_and_inclusive():
    raw, coords = make_network()
    raw.loc[raw.index[30:40], "S2"] = np.nan  # 10 h
    assert (rows(run(raw, coords, max_fill_hours=10), "S2").iloc[30:40]["source"] == "neighbour_fill").all()
    assert (rows(run(raw, coords, max_fill_hours=9), "S2").iloc[30:40]["source"] == "unfilled").all()


def test_leading_and_trailing_blanks_are_not_part_of_the_record():
    raw, coords = make_network()
    raw.loc[raw.index[:20], "S3"] = np.nan  # station starts reporting late
    raw.loc[raw.index[-10:], "S3"] = np.nan  # and stops early
    out = rows(run(raw, coords), "S3")
    assert len(out) == len(raw) - 30
    assert out["source"].isin(["observed"]).all()


def test_stuck_sensor_hours_are_replaced_and_labelled_as_such():
    raw, coords = make_network()
    raw.loc[raw.index[50:58], "S1"] = 999.0  # stuck for 8 h
    out = rows(run(raw, coords), "S1")
    stuck = out.iloc[50:58]
    assert (stuck["source"] == "stuck_replaced").all()
    assert (stuck["pm25_measured"] == 999.0).all()  # the bad measurement is preserved for audit...
    assert (stuck["pm25_final"] < 200).all()  # ...but is not what ends up in the final series


def test_stuck_hours_with_no_neighbours_reporting_are_removed_not_kept():
    raw, coords = make_network()
    raw.loc[raw.index[50:58], "S1"] = 999.0
    for other in ("S0", "S2", "S3", "S4"):  # nobody to estimate from
        raw.loc[raw.index[50:58], other] = np.nan
    out = rows(run(raw, coords), "S1")
    assert (out.iloc[50:58]["source"] == "stuck_removed").all()
    assert out.iloc[50:58]["pm25_final"].isna().all()


def test_gap_with_too_few_neighbours_reporting_stays_unfilled():
    raw, coords = make_network()
    raw.loc[raw.index[40:44], "S2"] = np.nan
    for other in ("S0", "S1", "S3", "S4"):
        raw.loc[raw.index[40:44], other] = np.nan
    assert (rows(run(raw, coords), "S2").iloc[40:44]["source"] == "unfilled").all()


def test_expected_error_pct_comes_from_the_backtest_file(tmp_path):
    bt = pd.DataFrame({"block_hours": [6, 24, 6], "method": ["neighbours", "neighbours", "persistence"],
                       "mae_ugm3": [12.0, 10.0, 99.0], "mean_truth_ugm3": [50.0, 50.0, 50.0]})
    path = tmp_path / "bt.csv"
    bt.to_csv(path, index=False)
    assert bf.expected_error_pct(path) == pytest.approx(22.0)  # mean of 24% and 20%; persistence row ignored
    assert bf.expected_error_pct(tmp_path / "missing.csv") == bf.DEFAULT_ERROR_PCT

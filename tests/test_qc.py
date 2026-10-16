import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_neighbor_consistency as nc  # noqa: E402
import qc  # noqa: E402


def test_flatline_mask_marks_every_hour_of_a_run_including_the_first():
    v = np.array([3, 5, 5, 5, 5, 5, 5, 8, 9], float)  # six 5s in a row
    assert list(qc.flatline_mask(v, 6)) == [False, True, True, True, True, True, True, False, False]


def test_flatline_mask_ignores_runs_shorter_than_the_threshold_and_nans():
    v = np.array([5, 5, 5, 5, 5, 9, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, 7], float)
    assert not qc.flatline_mask(v, 6).any()  # five 5s is too short; a NaN run is missing data, not a stuck sensor


def test_flatline_mask_handles_two_separate_runs():
    v = np.r_[np.full(7, 4.0), 9.0, np.full(6, 2.0)]
    m = qc.flatline_mask(v, 6)
    assert m[:7].all() and not m[7] and m[8:].all()


def test_mask_flatlines_blanks_only_the_stuck_hours_in_the_stuck_column():
    idx = pd.date_range("2026-10-05", periods=12, freq="h")
    pm = pd.DataFrame({"stuck": [50.0] * 8 + [51.0, 52.0, 53.0, 54.0], "fine": np.arange(1.0, 13.0)}, index=idx)
    out = qc.mask_flatlines(pm, 6)
    assert out["stuck"].iloc[:8].isna().all() and out["stuck"].iloc[8:].notna().all()
    assert out["fine"].equals(pm["fine"])
    assert pm["stuck"].notna().all()  # the input is not modified


def _long(station, parameter, values, start="2026-10-05T00:00:00+05:30"):
    ts = pd.date_range(pd.Timestamp(start[:19]), periods=len(values), freq="h")
    return pd.DataFrame({"hour_start_ist": [t.strftime("%Y-%m-%dT%H:%M:%S+05:30") for t in ts],
                         "station": station, "parameter": parameter, "value": values})


def test_drop_flatlined_hours_removes_stuck_pm_hours_and_reports_them():
    stuck = _long("A", "pm25", [42.0] * 8 + [10.0, 11.0, 12.0, 13.0])
    ok = _long("B", "pm25", list(np.arange(10.0, 22.0)))
    cleaned, report = qc.drop_flatlined_hours(pd.concat([stuck, ok], ignore_index=True))
    assert len(cleaned[cleaned["station"] == "A"]) == 4  # the 8 stuck hours are gone
    assert len(cleaned[cleaned["station"] == "B"]) == 12
    assert report.to_dict("records") == [{"station": "A", "parameter": "pm25", "hours_removed": 8}]


def test_drop_flatlined_hours_leaves_other_parameters_alone():
    stuck_pm = _long("A", "pm25", [42.0] * 10)
    wind_constant = _long("A", "wind_speed", [1.0] * 10)  # a constant wind reading is not our concern here
    cleaned, _ = qc.drop_flatlined_hours(pd.concat([stuck_pm, wind_constant], ignore_index=True))
    assert (cleaned["parameter"] == "wind_speed").sum() == 10
    assert (cleaned["parameter"] == "pm25").sum() == 0


def test_drop_flatlined_hours_is_a_noop_on_clean_data():
    clean = pd.concat([_long("A", "pm25", list(np.arange(1.0, 13.0))), _long("B", "pm10", list(np.arange(2.0, 14.0)))], ignore_index=True)
    cleaned, report = qc.drop_flatlined_hours(clean)
    assert len(cleaned) == len(clean) and report.empty


def _stuck_neighbour_setup():
    rng = np.random.default_rng(0)
    n = 96
    idx = pd.date_range("2026-10-05", periods=n, freq="h")
    common = np.log(60) + 0.4 * np.sin(np.arange(n) / 6)
    cols = {f"S{i}": np.exp(common + rng.normal(scale=0.05, size=n)) for i in range(4)}
    pm = pd.DataFrame(cols, index=idx)
    pm.loc[idx[20:60], "S1"] = 900.0  # S1 is stuck far too high for 40 h
    coords = pd.DataFrame({"station": list(cols), "latitude": 28.5 + 0.02 * np.arange(4), "longitude": 77.1})
    return pm, nc.build_neighbors(coords, list(cols))


def _target_error(frame, nb, predictor):
    logp = np.log(frame.to_numpy(float))
    zeros = np.zeros(frame.shape)
    pred = nc.predict_all(logp, nb, zeros, zeros)[predictor]
    corrected, _ = nc.offset_corrected(logp, pred)
    return np.nanmean(np.abs(logp[:, 0] - corrected[:, 0]))


def test_median_predictor_already_shrugs_off_one_stuck_neighbour():
    # Documents a measured fact: with the median predictor, masking changes almost nothing.
    pm, nb = _stuck_neighbour_setup()
    raw, masked = _target_error(pm, nb, "knn_median"), _target_error(qc.mask_flatlines(pm), nb, "knn_median")
    assert abs(raw - masked) < 0.02


def test_mean_based_predictor_is_corrupted_by_a_stuck_neighbour_until_masked():
    pm, nb = _stuck_neighbour_setup()
    raw, masked = _target_error(pm, nb, "idw_mean"), _target_error(qc.mask_flatlines(pm), nb, "idw_mean")
    assert raw > 0.25  # one stuck station drags a mean-based prediction far off
    assert masked < 0.10 and masked < raw / 3

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_stubble_transport as ast_  # noqa: E402
from windfield import build_wind_field  # noqa: E402


# ---------------------------------------------------------------- multi-covariate partial correlation


def test_partial_spearman_removes_two_confounders_that_act_differently_on_x_and_y():
    rng = np.random.default_rng(0)
    n = 80
    z1, z2 = rng.normal(size=n), rng.normal(size=n)
    x = z1 + z2 + rng.normal(scale=0.4, size=n)
    y = 2 * z1 - z2 + rng.normal(scale=0.4, size=n)
    raw, _ = ast_.permutation_spearman(x, y, n_perm=300)
    assert raw > 0.2  # correlated through the shared drivers...
    part, p = ast_.partial_spearman(x, y, np.column_stack([z1, z2]), n_perm=2000)
    assert abs(part) < 0.25 and p > 0.05  # ...and not once both are removed


def test_controlling_for_two_confounders_removes_more_than_controlling_for_one():
    rng = np.random.default_rng(1)
    n = 120
    z1, z2 = rng.normal(size=n), rng.normal(size=n)
    x = z1 + 2 * z2 + rng.normal(scale=0.3, size=n)
    y = z1 + 2 * z2 + rng.normal(scale=0.3, size=n)
    one, _ = ast_.partial_spearman(x, y, z1, n_perm=300)
    both, _ = ast_.partial_spearman(x, y, np.column_stack([z1, z2]), n_perm=300)
    assert one > 0.9 and both < one - 0.2


def test_known_limit_a_shared_nonlinear_dependence_survives_the_rank_linear_control():
    # DOCUMENTS A LIMITATION, not a feature: x and y depend on (z1, z2) through the same nonlinear
    # function, so a rank-linear control cannot remove all of it. The partial correlation stays high
    # even though, given z, x and y are independent. Findings built on this control must say so.
    rng = np.random.default_rng(1)
    n = 120
    z1, z2 = rng.normal(size=n), rng.normal(size=n)
    x = z1 + 2 * z2 + rng.normal(scale=0.3, size=n)
    y = z1 + 2 * z2 + rng.normal(scale=0.3, size=n)
    both, _ = ast_.partial_spearman(x, y, np.column_stack([z1, z2]), n_perm=300)
    assert both > 0.4  # NOT ~0: this is the residual-confounding risk


def test_partial_spearman_still_accepts_a_single_1d_covariate():
    rng = np.random.default_rng(2)
    z = rng.normal(size=50)
    x, y = z + rng.normal(scale=0.3, size=50), z + rng.normal(scale=0.3, size=50)
    rho, p = ast_.partial_spearman(x, y, z, n_perm=500)
    assert np.isfinite(rho) and 0 < p <= 1


def test_partial_circular_shift_p_cannot_beat_one_over_n_and_handles_degenerate_input():
    rng = np.random.default_rng(3)
    z = rng.normal(size=14)
    x = np.arange(14.0) + rng.normal(scale=0.1, size=14)
    rho, p = ast_.partial_circular_shift_p(x, x + rng.normal(scale=0.1, size=14), z)
    assert p >= 1 / 14 - 1e-9
    r2, p2 = ast_.partial_circular_shift_p(np.ones(14), np.arange(14.0), z)
    assert np.isnan(r2) and np.isnan(p2)  # regression: constant input left ~1e-15 noise that std == 0 missed
    assert np.isnan(ast_.partial_spearman(np.ones(14), np.arange(14.0), z, n_perm=50)[0])
    assert np.isnan(ast_.partial_circular_shift_p(np.arange(3.0), np.arange(3.0), np.arange(3.0))[0])


# ---------------------------------------------------------------- Delhi daily meteorology


def _era5(blh_by_hour, near_value=1.0, far_blh=9999.0, dates=("2026-10-05",), drop_hours=()):
    rows = []
    for d in dates:
        for h in range(0, 24, 3):
            if h in drop_hours:
                continue
            t = pd.Timestamp(f"{d} {h:02d}:00")
            rows.append((t, 28.5, 77.25, blh_by_hour[h] * near_value, 300.0))  # inside the 60 km receptor
            rows.append((t, 31.5, 74.0, far_blh, 280.0))  # Punjab: must be excluded from Delhi's average
    return pd.DataFrame(rows, columns=["time", "latitude", "longitude", "blh_m", "t2m_k"])


BLH = {0: 50, 3: 300, 6: 900, 9: 1700, 12: 100, 15: 80, 18: 60, 21: 50}


def test_delhi_daily_met_uses_only_cells_near_delhi_and_takes_the_afternoon_max():
    out = ast_.delhi_daily_met(_era5(BLH))
    # UTC 00:00..18:00 on 10-05 fall on IST 10-05 (05:30..23:30); UTC 21:00 is 02:30 IST on 10-06
    day = out[out["date"] == "2026-10-05"].iloc[0]
    assert day["blh_delhi_max"] == 1700  # Punjab's 9999 was ignored
    assert day["t2m_delhi"] == pytest.approx(300.0)


def test_delhi_daily_met_buckets_by_ist_day_not_utc_day():
    out = ast_.delhi_daily_met(_era5(BLH, dates=("2026-10-05", "2026-10-06")), min_steps=1)
    assert list(out["date"]) == ["2026-10-05", "2026-10-06", "2026-10-07"]  # the last UTC 21:00 lands on IST 10-07
    first = out.iloc[0]
    assert first["blh_delhi_max"] == 1700


def test_delhi_daily_met_drops_days_with_too_few_timestamps():
    out = ast_.delhi_daily_met(_era5(BLH, drop_hours=(0, 3, 6)), min_steps=7)
    assert "2026-10-05" not in set(out["date"])


def test_delhi_daily_met_without_the_new_columns_returns_an_empty_frame_not_an_error():
    old = pd.DataFrame({"time": ["2026-10-05 00:00"], "latitude": [28.5], "longitude": [77.2], "u10_ms": [1.0]})
    out = ast_.delhi_daily_met(old)
    assert out.empty and list(out.columns) == ["date", "blh_delhi_max", "t2m_delhi"]


# ---------------------------------------------------------------- 925 hPa wind field and association table


def test_build_wind_field_can_read_the_925_hpa_columns():
    times = pd.date_range("2026-10-01", periods=3, freq="3h")
    rows = [(t, la, lo, 1.0, 1.0, 9.0, -4.0) for t in times for la in (28.0, 29.0) for lo in (77.0, 78.0)]
    df = pd.DataFrame(rows, columns=["time", "latitude", "longitude", "u10_ms", "v10_ms", "u925_ms", "v925_ms"])
    low, high = build_wind_field(df), build_wind_field(df, "u925_ms", "v925_ms")
    assert np.nanmean(low.u) == 1.0 and np.nanmean(high.u) == 9.0 and np.nanmean(high.v) == -4.0


def _daily(n=16, seed=0, with_met=True):
    rng = np.random.default_rng(seed)
    blh = rng.uniform(300, 2000, n)
    temp = np.linspace(305, 295, n) + rng.normal(scale=0.5, size=n)
    exposure = np.r_[np.zeros(4), rng.uniform(1, 10, n - 4)]
    pm = 100 - 0.03 * blh + rng.normal(scale=4, size=n) + 3 * exposure
    d = pd.DataFrame({"date": [f"2026-10-{i + 1:02d}" for i in range(n)], "pm25_delhi": pm, "wind_delhi": rng.uniform(0.5, 1.5, n), "e": exposure})
    if with_met:
        d["blh_delhi_max"], d["t2m_delhi"] = blh, temp
    return d


def test_association_table_reports_the_boundary_layer_control_when_met_is_available():
    row = ast_.association_table(_daily(), ["e"]).query("lag_days == 0").iloc[0]
    assert row["partial_ctrl_blh_temp"] is not None and np.isfinite(row["partial_ctrl_blh_temp"])
    assert row["partial_blh_perm_p"] is not None and row["partial_blh_circ_p"] >= 1 / 16 - 1e-3


def test_association_table_still_works_without_met_columns():
    row = ast_.association_table(_daily(with_met=False), ["e"]).query("lag_days == 0").iloc[0]
    assert row["partial_ctrl_blh_temp"] is None and row["partial_blh_perm_p"] is None
    assert np.isfinite(row["spearman"])


def test_controlling_for_boundary_layer_changes_the_picture_when_it_is_the_real_driver():
    # PM2.5 is driven ONLY by boundary-layer height; the "exposure" merely tracks it (both fall on
    # stagnant days). Raw correlation looks impressive; controlling for BLH must remove it.
    rng = np.random.default_rng(5)
    n = 30
    blh = rng.uniform(300, 2000, n)
    exposure = (2000 - blh) / 100 + rng.normal(scale=1.0, size=n)
    pm = 150 - 0.05 * blh + rng.normal(scale=3, size=n)
    d = pd.DataFrame({"date": [f"2026-10-{i + 1:02d}" for i in range(n)], "pm25_delhi": pm, "e": exposure,
                      "blh_delhi_max": blh, "t2m_delhi": rng.normal(300, 3, n)})
    row = ast_.association_table(d, ["e"]).query("lag_days == 0").iloc[0]
    assert row["spearman"] > 0.6
    assert abs(row["partial_ctrl_blh_temp"]) < 0.35

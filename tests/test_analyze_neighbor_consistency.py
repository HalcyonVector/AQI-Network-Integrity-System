import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_neighbor_consistency as nc  # noqa: E402
from windfield import build_wind_field  # noqa: E402


# ---------------------------------------------------------------- synthetic network


def make_network(n_side=3, n_hours=24 * 14, seed=0, noise=0.08):
    """A grid of stations ~3 km apart sharing one citywide log-PM2.5 signal plus a
    persistent per-station offset and small noise. Returns (pm DataFrame, coords DataFrame)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-10-01", periods=n_hours, freq="h")
    t = np.arange(n_hours)
    common = np.log(70) + 0.5 * np.sin(2 * np.pi * t / 24) + np.cumsum(rng.normal(scale=0.03, size=n_hours))
    rows, cols = [], {}
    for a in range(n_side):
        for b in range(n_side):
            name = f"S{a}{b}"
            rows.append({"station": name, "latitude": 28.5 + 0.03 * a, "longitude": 77.1 + 0.03 * b})
            cols[name] = np.exp(common + rng.normal(scale=0.3) + rng.normal(scale=noise, size=n_hours))
    return pd.DataFrame(cols, index=idx), pd.DataFrame(rows)


def calm(pm):
    z = np.zeros(pm.shape)
    return z, z  # speed, wind-from


def run_pipeline(pm, coords, speed=None, wfrom=None):
    stations = list(pm.columns)
    nb = nc.build_neighbors(coords, stations)
    if speed is None:
        speed, wfrom = calm(pm)
    logp = np.log(pm.to_numpy(float))
    return nb, nc.predict_all(logp, nb, speed, wfrom), logp


# ---------------------------------------------------------------- geometry


def test_bearing_cardinal_directions():
    assert nc.bearing_deg(28.0, 77.0, 29.0, 77.0) == pytest.approx(0.0, abs=0.01)
    assert nc.bearing_deg(28.0, 77.0, 28.0, 78.0) == pytest.approx(90.0, abs=0.5)
    assert nc.bearing_deg(28.0, 77.0, 27.0, 77.0) == pytest.approx(180.0, abs=0.01)
    assert nc.bearing_deg(28.0, 77.0, 28.0, 76.0) == pytest.approx(270.0, abs=0.5)


def test_build_neighbors_limits_k_excludes_self_and_respects_radius():
    coords = pd.DataFrame({"station": ["A", "B", "C", "D"], "latitude": [28.5, 28.51, 28.52, 31.0], "longitude": [77.1, 77.1, 77.1, 77.1]})
    nb = nc.build_neighbors(coords, ["A", "B", "C", "D"], radius_km=25, k=1)
    assert list(nb[0]["idx"]) == [1]  # A's single nearest neighbour is B, not itself
    nb = nc.build_neighbors(coords, ["A", "B", "C", "D"], radius_km=25, k=6)
    assert 0 not in nb[0]["idx"] and 3 not in nb[0]["idx"]  # D is ~280 km away
    assert len(nb[3]["idx"]) == 0  # D has nobody within range


def test_build_neighbors_handles_station_without_coordinates():
    coords = pd.DataFrame({"station": ["A", "B"], "latitude": [28.5, np.nan], "longitude": [77.1, np.nan]})
    nb = nc.build_neighbors(coords, ["A", "B"])
    assert len(nb[0]["idx"]) == 0 and len(nb[1]["idx"]) == 0


# ---------------------------------------------------------------- predictors


def test_neighbour_prediction_is_accurate_on_a_clean_network():
    pm, coords = make_network()
    _, preds, logp = run_pipeline(pm, coords)
    corrected, _ = nc.offset_corrected(logp, preds["knn_median"])
    assert np.nanmean(np.abs(logp - corrected)) < 0.12


def test_prediction_is_leave_one_out():
    # Corrupting a station's OWN reading must not change its own prediction.
    pm, coords = make_network()
    _, base, logp = run_pipeline(pm, coords)
    bad = logp.copy()
    bad[50, 0] += 5.0
    nb = nc.build_neighbors(coords, list(pm.columns))
    after = nc.predict_all(bad, nb, *calm(pm))
    assert after["knn_median"][50, 0] == base["knn_median"][50, 0]


def test_too_few_neighbours_gives_nan_not_a_guess():
    pm, coords = make_network(n_side=2)  # 4 stations -> 3 neighbours each: just enough
    _, preds, logp = run_pipeline(pm, coords)
    assert np.isfinite(preds["knn_median"]).all()
    logp2 = logp.copy()
    logp2[10, 1:] = np.nan  # at hour 10 only station 0's own value exists, no neighbours
    nb = nc.build_neighbors(coords, list(pm.columns))
    out = nc.predict_all(logp2, nb, *calm(pm))
    assert np.isnan(out["knn_median"][10, 0])


def _line_up(values_west, values_east):
    """A centre station with 2 neighbours due west and 2 due east, ~2 km out."""
    names = ["C", "W1", "W2", "E1", "E2"]
    coords = pd.DataFrame({
        "station": names,
        "latitude": [28.6] * 5,
        "longitude": [77.2, 77.18, 77.17, 77.22, 77.23],
    })
    T = 3
    logp = np.zeros((T, 5))
    logp[:, 1:3] = values_west
    logp[:, 3:5] = values_east
    return names, coords, logp


def test_upwind_rule_uses_only_neighbours_on_the_windward_side():
    names, coords, logp = _line_up(values_west=2.0, values_east=5.0)
    nb = nc.build_neighbors(coords, names, k=4)
    speed = np.full((3, 5), 3.0)
    wfrom = np.full((3, 5), 270.0)  # wind FROM the west -> west neighbours are upwind
    out = nc.predict_all(logp, nb, speed, wfrom)
    assert out["used_upwind"][0, 0]
    assert out["upwind_median"][0, 0] == pytest.approx(2.0)
    assert out["knn_median"][0, 0] == pytest.approx(3.5)  # all four: median of 2,2,5,5

    wfrom_e = np.full((3, 5), 90.0)  # from the east -> the other pair
    assert nc.predict_all(logp, nb, speed, wfrom_e)["upwind_median"][0, 0] == pytest.approx(5.0)


def test_upwind_rule_falls_back_to_all_neighbours_in_calm_air():
    names, coords, logp = _line_up(2.0, 5.0)
    nb = nc.build_neighbors(coords, names, k=4)
    out = nc.predict_all(logp, nb, np.full((3, 5), 0.5), np.full((3, 5), 270.0))
    assert not out["used_upwind"][0, 0]
    assert out["upwind_median"][0, 0] == pytest.approx(out["knn_median"][0, 0])


def test_upwind_rule_needs_at_least_two_upwind_reporters():
    names, coords, logp = _line_up(2.0, 5.0)
    logp[:, 2] = np.nan  # one of the two west neighbours stops reporting
    nb = nc.build_neighbors(coords, names, k=4)
    out = nc.predict_all(logp, nb, np.full((3, 5), 3.0), np.full((3, 5), 270.0))
    assert not out["used_upwind"][0, 0]


def test_idw_weights_nearer_neighbours_more():
    names, coords, logp = _line_up(2.0, 5.0)  # W1 (2.0) is nearest at ~1.9 km, E-side farther
    coords.loc[coords.station == "E1", "longitude"] = 77.30  # push E1 far away
    coords.loc[coords.station == "E2", "longitude"] = 77.31
    nb = nc.build_neighbors(coords, names, k=4)
    out = nc.predict_all(logp, nb, np.zeros((3, 5)), np.zeros((3, 5)))
    assert out["idw_mean"][0, 0] < out["knn_median"][0, 0]  # pulled toward the near west pair (2.0)


def test_offset_correction_removes_persistent_site_offset_and_honours_exclusions():
    rng = np.random.default_rng(0)
    pred = rng.normal(size=(100, 1))
    logp = pred + 0.7
    corrected, off = nc.offset_corrected(logp, pred)
    assert off[0] == pytest.approx(0.7)
    assert np.allclose(corrected, logp)
    # a block of wild values must not move the offset when excluded
    logp2 = logp.copy()
    logp2[40:60, 0] += 9.0
    exclude = np.zeros_like(logp2, dtype=bool)
    exclude[40:60, 0] = True
    assert nc.offset_corrected(logp2, pred, exclude)[1][0] == pytest.approx(0.7)


# ---------------------------------------------------------------- inputs


def test_pm_matrix_fills_hour_gaps_and_drops_implausible_values():
    rows = [
        {"hour_start_ist": "2026-10-05T00:00:00+05:30", "station": "A", "parameter": "pm25", "value": 50.0},
        {"hour_start_ist": "2026-10-05T03:00:00+05:30", "station": "A", "parameter": "pm25", "value": 99999.0},  # glitch
        {"hour_start_ist": "2026-10-05T04:00:00+05:30", "station": "A", "parameter": "pm25", "value": -3.0},  # negative
        {"hour_start_ist": "2026-10-05T05:00:00+05:30", "station": "A", "parameter": "pm25", "value": 60.0},
        {"hour_start_ist": "2026-10-05T05:00:00+05:30", "station": "A", "parameter": "pm10", "value": 500.0},  # other parameter
    ]
    m = nc.pm_matrix(pd.DataFrame(rows))
    assert len(m) == 6  # complete hourly grid 00:00..05:00
    assert m.iloc[0, 0] == 50.0 and m.iloc[5, 0] == 60.0
    assert m.iloc[1:5, 0].isna().all()


def test_station_wind_returns_speed_and_meteorological_from_direction():
    times = pd.date_range("2026-10-01", periods=5, freq="3h")
    rows = [(t, la, lo, 3.0, 0.0) for t in times for la in (28.0, 29.0) for lo in (77.0, 78.0)]  # blowing EAST at 3 m/s
    wind = build_wind_field(pd.DataFrame(rows, columns=["time", "latitude", "longitude", "u10_ms", "v10_ms"]))
    idx = pd.date_range("2026-10-01 06:00", periods=3, freq="h") + nc.IST_OFFSET  # IST labels for UTC 06:00..08:00
    coords = pd.DataFrame({"station": ["A"], "latitude": [28.5], "longitude": [77.5]})
    speed, wfrom = nc.station_wind(idx, coords, ["A"], wind)
    assert speed[:, 0] == pytest.approx([3.0] * 3)
    assert wfrom[:, 0] == pytest.approx([270.0] * 3)  # from the west


# ---------------------------------------------------------------- health checks


def test_run_lengths_and_flatline_runs():
    assert nc.run_lengths(np.array([0, 1, 1, 0, 1, 1, 1], bool)) == [(1, 2), (4, 3)]
    v = np.array([5, 5, 5, 5, 5, 5, 5, 5, 9, 4, 4, 4, 4, 4, 1, np.nan, np.nan, 7], float)
    assert nc.flatline_runs(v, min_hours=6) == [(0, 8)]  # the run of 5 fours is too short; NaNs never count
    assert nc.flatline_runs(np.array([1.0, 2.0, 3.0])) == []


def test_benjamini_hochberg_is_bounded_and_monotone():
    p = np.array([0.001, 0.8, 0.02, 0.5, 0.04, 0.3])
    q = nc.benjamini_hochberg(p)
    assert np.all(q >= p - 1e-12) and np.all(q <= 1)
    order = np.argsort(p)
    assert np.all(np.diff(q[order]) >= -1e-12)


def _health(pm, coords):
    _, preds, _ = run_pipeline(pm, coords)
    return nc.station_health(pm, preds["knn_median"]).set_index("station")


def test_clean_network_has_no_health_flags():
    for seed in range(4):
        pm, coords = make_network(n_side=4, seed=seed)
        h = _health(pm, coords)
        assert not h["flag"].any(), f"false positive with seed {seed}: {h[h['flag']]['reason'].to_dict()}"


def test_flatline_is_flagged():
    pm, coords = make_network(n_side=4, seed=1)
    pm.loc[pm.index[100:112], "S11"] = 42.0  # stuck at one value for 12 hours
    h = _health(pm, coords)
    assert h.loc["S11", "flag"] and "flatline" in h.loc["S11", "reason"]
    assert h.loc["S11", "flatline_hours"] >= 12
    assert not h.drop(index="S11")["flag"].any()  # and nobody else


def test_sustained_departure_from_neighbours_is_flagged():
    pm, coords = make_network(n_side=4, seed=2)
    pm.loc[pm.index[150:190], "S22"] *= 3.0  # reads 3x high for 40 hours
    h = _health(pm, coords)
    assert h.loc["S22", "flag"] and "sustained departure" in h.loc["S22", "reason"]
    assert h.loc["S22", "anomaly_hours"] >= 30


def test_slow_drift_is_flagged_by_trend():
    pm, coords = make_network(n_side=4, seed=3)
    ramp = np.exp(np.linspace(0, 1.2, len(pm)))  # a sensor creeping up to 3.3x over the period
    pm["S13"] = pm["S13"] * ramp
    h = _health(pm, coords)
    assert h.loc["S13", "trend_log_per_day"] > 0.05
    assert h.loc["S13", "trend_q"] < nc.FDR
    assert h.loc["S13", "flag"] and "drifting" in h.loc["S13", "reason"] and "%" in h.loc["S13", "reason"]


def test_station_with_too_little_data_is_reported_not_judged():
    pm, coords = make_network(n_side=3)
    pm.iloc[:, 0] = np.nan
    pm.iloc[:20, 0] = 50.0
    h = _health(pm, coords)
    assert h.iloc[:, :].loc["S00", "reason"].startswith("insufficient")
    assert not h.loc["S00", "flag"]


# ---------------------------------------------------------------- comparison and backtest


def test_compare_predictors_returns_regimes_and_paired_summary():
    pm, coords = make_network(n_side=4)
    speed = np.full(pm.shape, 3.0)
    wfrom = np.full(pm.shape, 270.0)
    nb = nc.build_neighbors(coords, list(pm.columns))
    logp = np.log(pm.to_numpy(float))
    preds = nc.predict_all(logp, nb, speed, wfrom)
    table, paired = nc.compare_predictors(logp, preds, speed, list(pm.columns))
    assert set(table["predictor"]) == {"knn_median", "idw_mean", "upwind_median"}
    assert "windy (>=1.5 m/s)" in set(table["regime"])
    assert paired["n_stations"] > 0
    assert 0 <= paired["stations_improved"] <= paired["n_stations"]


def test_gapfill_backtest_beats_persistence_on_long_gaps_and_is_reproducible():
    pm, coords = make_network(n_side=4, seed=5)
    _, preds, _ = run_pipeline(pm, coords)
    bt = nc.backtest_gapfill(pm, preds, block_lengths=(6, 24), trials_per_length=80, seed=1)
    assert set(bt["block_hours"]) == {6, 24}
    assert set(bt["method"]) == {"neighbours", "persistence", "yesterday"}
    long = bt[bt["block_hours"] == 24].set_index("method")["mae_ugm3"]
    assert long["neighbours"] < long["persistence"]
    again = nc.backtest_gapfill(pm, preds, block_lengths=(6, 24), trials_per_length=80, seed=1)
    pd.testing.assert_frame_equal(bt, again)


def test_autocorrelation_adjustment_reduces_but_does_not_eliminate_false_positives():
    # Trendless but strongly autocorrelated series: plain Kendall cries "trend" most of the time.
    # Measured: ~60% -> ~35% at n=17. Better, still far above the nominal 5% -- which is exactly why
    # drift alone only becomes a FLAG when its magnitude is large (see MIN_TOTAL_DRIFT_LOG).
    rng = np.random.default_rng(1)
    raw_hits = adj_hits = wn_adj_hits = 0
    trials = 400
    for _ in range(trials):
        r = nc.trend_test(np.cumsum(rng.normal(size=17)))
        raw_hits += r["p_raw"] < 0.05
        adj_hits += r["p_adj"] < 0.05
        wn_adj_hits += nc.trend_test(rng.normal(size=17))["p_adj"] < 0.05
    assert adj_hits < 0.8 * raw_hits  # materially better than the unadjusted test
    assert adj_hits / trials > 0.15  # ...and the test documents that it is still inflated
    assert wn_adj_hits / trials < 0.09  # white noise: the adjustment does not make things worse


def test_trend_adjustment_never_strengthens_the_evidence():
    rng = np.random.default_rng(2)
    for _ in range(60):
        r = nc.trend_test(np.cumsum(rng.normal(size=15)))
        assert r["p_adj"] >= r["p_norm"] - 1e-12


def test_large_drift_is_a_flag_but_moderate_significant_drift_is_only_a_watch():
    pm, coords = make_network(n_side=4, seed=3)
    big = pm.copy()
    big["S13"] = big["S13"] * np.exp(np.linspace(0, 1.2, len(big)))
    h = _health(big, coords)
    assert h.loc["S13", "flag"] and not h.loc["S13", "watch"]

    moderate = pm.copy()
    moderate["S13"] = moderate["S13"] * np.exp(np.linspace(0, 0.35, len(moderate)))  # 0.025 log/day, 0.35 total
    h = _health(moderate, coords)
    assert h.loc["S13", "watch"] and not h.loc["S13", "flag"]
    assert h.loc["S13", "reason"].startswith("watch:")
def test_statistically_significant_but_tiny_drift_is_not_flagged():
    # Regression: a -0.003 log/day trend (q = 0.05) was flagged on a clean network.
    pm, coords = make_network(n_side=4, seed=0)
    h = _health(pm, coords)
    tiny = h[(h["trend_q"] < nc.FDR) & (h["trend_log_per_day"].abs() < nc.MIN_TREND_LOG_PER_DAY)]
    assert len(tiny) >= 1, "seed no longer produces the significant-but-tiny case this test pins"
    assert not tiny["flag"].any()


def test_a_genuinely_noisy_station_is_flagged_but_ordinary_variation_is_not():
    pm, coords = make_network(n_side=4, seed=2)  # seed 2 once flagged an ordinary station as noisy
    assert not _health(pm, coords)["flag"].any()
    rng = np.random.default_rng(9)
    pm["S30"] = pm["S30"] * np.exp(rng.normal(scale=0.5, size=len(pm)))  # ~6x the others' noise
    h = _health(pm, coords)
    assert h.loc["S30", "flag"] and "noisy" in h.loc["S30", "reason"]


def test_trend_test_finds_a_real_trend_and_adjusts_for_autocorrelation():
    rng = np.random.default_rng(0)
    y = np.linspace(0, 1.5, 18) + rng.normal(scale=0.1, size=18)
    r = nc.trend_test(y)
    assert r["slope"] > 0.05 and r["p_adj"] < 0.01




# ---------------------------------------------------------------- classification tiers


def _row(**kw):
    base = {"resid_sigma_log": 0.1, "flatline_episodes": 0, "flatline_hours": 0, "anomaly_episodes": 0, "anomaly_hours": 0,
            "trend_log_per_day": 0.0, "trend_days": 17, "trend_p": 0.5, "trend_q": 0.9, "reason": ""}
    base.update(kw)
    return pd.Series(base)


def test_classify_clean_station_is_ok():
    assert nc.classify_station(_row(), noisy_cut=0.3) == ("consistent with neighbours", "ok")


def test_classify_flatline_and_noise_are_flags():
    reason, level = nc.classify_station(_row(flatline_episodes=2, flatline_hours=14), noisy_cut=0.3)
    assert level == "flag" and "flatline x2 (14 h)" in reason
    assert nc.classify_station(_row(resid_sigma_log=0.5), noisy_cut=0.3)[1] == "flag"


def test_classify_significant_large_drift_is_a_flag():
    reason, level = nc.classify_station(_row(trend_log_per_day=-0.08, trend_p=0.001, trend_q=0.02), noisy_cut=0.3)
    assert level == "flag" and "drifting" in reason and "%" in reason


def test_classify_significant_but_small_drift_is_only_a_watch():
    # 0.025 log/day over 17 days = 0.40 total: below the 0.5 flag bar
    reason, level = nc.classify_station(_row(trend_log_per_day=0.025, trend_p=0.001, trend_q=0.02), noisy_cut=0.3)
    assert level == "watch" and reason.startswith("watch:")


def test_classify_large_apparent_drift_not_surviving_fdr_is_a_watch_that_says_so():
    # The IHBAS pattern: halving+ vs neighbours, p=0.009 on its own, q=0.5 across ~55 stations.
    reason, level = nc.classify_station(_row(trend_log_per_day=-0.064, trend_p=0.009, trend_q=0.5), noisy_cut=0.3)
    assert level == "watch"
    assert "not confirmed after multiple-testing correction" in reason


def test_classify_does_not_watch_a_large_drift_that_is_not_even_individually_significant():
    # The Sector-51 pattern: big slope but a wildly persistent series (p=0.68).
    assert nc.classify_station(_row(trend_log_per_day=-0.062, trend_p=0.68, trend_q=0.86), noisy_cut=0.3)[1] == "ok"


def test_classify_does_not_watch_a_small_drift_even_if_individually_significant():
    # -0.01 log/day over 17 days is ~15%: significant maybe, but nobody needs to look.
    assert nc.classify_station(_row(trend_log_per_day=-0.01, trend_p=0.01, trend_q=0.4), noisy_cut=0.3)[1] == "ok"


def test_classify_flag_text_carries_any_watch_notes_too():
    reason, level = nc.classify_station(
        _row(flatline_episodes=1, flatline_hours=8, trend_log_per_day=-0.064, trend_p=0.009, trend_q=0.5), noisy_cut=0.3)
    assert level == "flag" and "flatline" in reason and "watch: large apparent drift" in reason


def test_classify_insufficient_data_passes_the_reason_through():
    r = pd.Series({"resid_sigma_log": np.nan, "reason": "insufficient hours (20 < 72)"})
    assert nc.classify_station(r, noisy_cut=0.3) == ("insufficient hours (20 < 72)", "none")

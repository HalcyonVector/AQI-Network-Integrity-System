import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_neighbor_consistency as nc  # noqa: E402


def _stations(n=30, seed=0):
    rng = np.random.default_rng(seed)
    dist = rng.uniform(1.5, 14.0, n)
    sigma = 0.15 * dist**0.5 * np.exp(rng.normal(scale=0.08, size=n))  # noise grows with isolation
    idx = [f"S{i}" for i in range(n)]
    return pd.Series(sigma, index=idx), pd.Series(dist, index=idx)


def test_noise_that_simply_tracks_isolation_gives_ratios_near_one():
    sigma, dist = _stations()
    ratio = nc.isolation_adjusted_noise_ratio(sigma, dist)
    assert 0.85 < ratio.median() < 1.15
    assert ratio.max() < 1.4 and ratio.min() > 0.7


def test_an_isolated_station_is_not_noisy_just_for_being_isolated():
    # The first run flagged every isolated station. Here the most isolated one has the highest raw
    # noise, but exactly what its isolation predicts -> it must not be flagged.
    sigma, dist = _stations()
    far = dist.idxmax()
    assert sigma[far] > sigma.median() * 1.3  # raw noise alone would have looked alarming
    assert not nc.flag_noisy(nc.isolation_adjusted_noise_ratio(sigma, dist))[far]


def test_a_station_noisier_than_its_isolation_explains_is_flagged():
    sigma, dist = _stations()
    sigma["S3"] *= 2.5
    flagged = nc.flag_noisy(nc.isolation_adjusted_noise_ratio(sigma, dist))
    assert flagged["S3"] and flagged.sum() == 1


def test_a_close_station_with_ordinary_raw_noise_can_still_be_flagged():
    # The adjustment cuts both ways. The closest station has neighbours ~1.5 km away, so it should be
    # QUIET; giving it the network's median raw noise looks ordinary without adjustment but is more
    # than double what its closeness predicts.
    sigma, dist = _stations()
    close = dist.idxmin()
    sigma[close] = sigma.median()
    assert sigma[close] < sigma.median() * nc.NOISY_MIN_RATIO  # an unadjusted rule would call it fine
    adj = nc.isolation_adjusted_noise_ratio(sigma, dist)
    assert adj[close] > 2.0
    assert nc.flag_noisy(adj)[close]


def test_too_few_stations_falls_back_to_noise_relative_to_the_median():
    sigma = pd.Series([0.2, 0.3, 0.25], index=list("abc"))
    ratio = nc.isolation_adjusted_noise_ratio(sigma, pd.Series([2.0, 5.0, 8.0], index=list("abc")))
    assert ratio["c"] == pytest.approx(0.25 / 0.25) and ratio["b"] == pytest.approx(0.3 / 0.25)
    assert not nc.flag_noisy(ratio).any()  # and with <8 stations nothing is declared noisy


def test_stations_without_a_distance_are_judged_against_the_median():
    sigma, dist = _stations()
    dist["S5"] = np.nan
    ratio = nc.isolation_adjusted_noise_ratio(sigma, dist)
    assert ratio["S5"] == pytest.approx(sigma["S5"] / sigma.median())


def test_flag_noisy_requires_a_practically_large_ratio_not_just_a_statistical_outlier():
    # A perfectly uniform network has MAD ~ 0, so any tiny deviation is a "statistical outlier".
    ratio = pd.Series(np.r_[np.full(30, 1.0), 1.12], index=[f"S{i}" for i in range(31)])  # 12% above: not worth flagging
    assert not nc.flag_noisy(ratio).any()
    ratio.iloc[-1] = 2.0
    assert nc.flag_noisy(ratio).iloc[-1]


def test_station_health_with_neighbour_geometry_exposes_the_ratio_and_still_flags_real_noise():
    from test_analyze_neighbor_consistency import make_network, run_pipeline  # noqa: E402

    pm, coords = make_network(n_side=4, seed=2)
    nb, preds, _ = run_pipeline(pm, coords)
    clean = nc.station_health(pm, preds["knn_median"], nb)
    assert {"noise_ratio", "noisy", "mean_neighbor_km"} <= set(clean.columns)
    assert not clean["flag"].any()

    rng = np.random.default_rng(9)
    pm2 = pm.copy()
    pm2["S30"] = pm2["S30"] * np.exp(rng.normal(scale=0.5, size=len(pm2)))
    nb2, preds2, _ = run_pipeline(pm2, coords)
    h = nc.station_health(pm2, preds2["knn_median"], nb2).set_index("station")
    assert h.loc["S30", "flag"] and "isolation" in h.loc["S30", "reason"]


def test_station_health_without_geometry_still_works_the_old_way():
    from test_analyze_neighbor_consistency import make_network, run_pipeline  # noqa: E402

    pm, coords = make_network(n_side=4, seed=2)
    _, preds, _ = run_pipeline(pm, coords)
    out = nc.station_health(pm, preds["knn_median"])
    assert "noise_ratio" not in out.columns and not out["flag"].any()

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_stubble_transport as ast_  # noqa: E402


def uniform_wind(u, v, start="2026-10-01", hours=96, lat=(26.0, 32.0), lon=(73.0, 81.0), step=0.5):
    """A wind field with the same (u, v) everywhere, 3-hourly, built through the real
    DataFrame path (so build_wind_field is exercised too)."""
    times = pd.date_range(start, periods=hours // 3 + 1, freq="3h")
    lats = np.arange(lat[0], lat[1] + 1e-9, step)
    lons = np.arange(lon[0], lon[1] + 1e-9, step)
    rows = [(t, a, o, u, v) for t in times for a in lats for o in lons]
    return ast_.build_wind_field(pd.DataFrame(rows, columns=["time", "latitude", "longitude", "u10_ms", "v10_ms"]))


def fire(lat, lon, time="2026-10-01 06:00", frp=10.0):
    return pd.DataFrame({"fire_time": [pd.Timestamp(time)], "latitude": [lat], "longitude": [lon], "frp": [frp]})


KM_PER_DEG = 111.19
DELHI_LAT, DELHI_LON = ast_.DELHI
WEST_300KM = DELHI_LON - 300 / (KM_PER_DEG * np.cos(np.radians(DELHI_LAT)))


def test_upwind_fire_hits_delhi_at_the_expected_time():
    # Fire ~300 km due west of Delhi, wind blowing east at 5 m/s = 18 km/h.
    # 300 km - 60 km radius = 240 km to cover -> ~13 h.
    out = ast_.advect_fires(fire(DELHI_LAT, WEST_300KM), uniform_wind(5.0, 0.0))
    row = out.iloc[0]
    assert bool(row["hit"]) and not bool(row["local"])
    assert 11 <= row["travel_h"] <= 15
    assert row["arrival_time"] == row["fire_time"] + pd.Timedelta(hours=row["travel_h"])


def test_downwind_fire_never_hits():
    out = ast_.advect_fires(fire(DELHI_LAT, WEST_300KM), uniform_wind(-5.0, 0.0))
    assert not bool(out.iloc[0]["hit"])


def test_crosswind_fire_misses_but_reports_closest_approach():
    out = ast_.advect_fires(fire(DELHI_LAT - 1.0, WEST_300KM), uniform_wind(0.0, 4.0))
    assert not bool(out.iloc[0]["hit"])
    assert out.iloc[0]["min_dist_km"] > 100


def test_calm_air_means_no_transport():
    assert not bool(ast_.advect_fires(fire(DELHI_LAT, WEST_300KM), uniform_wind(0.0, 0.0)).iloc[0]["hit"])


def test_fire_already_inside_the_receptor_is_flagged_local_not_transport():
    out = ast_.advect_fires(fire(DELHI_LAT + 0.2, DELHI_LON + 0.2), uniform_wind(2.0, 0.0))
    assert bool(out.iloc[0]["local"]) and out.iloc[0]["travel_h"] == 0
    assert ast_.daily_exposure(out).empty  # local burning is not regional transport


def test_speed_factor_shortens_travel_time():
    w = uniform_wind(3.0, 0.0)
    slow = ast_.advect_fires(fire(DELHI_LAT, WEST_300KM), w, speed_factor=1.0).iloc[0]["travel_h"]
    fast = ast_.advect_fires(fire(DELHI_LAT, WEST_300KM), w, speed_factor=2.0).iloc[0]["travel_h"]
    assert fast < slow


def test_parcel_leaving_the_wind_domain_is_dropped_not_extrapolated():
    out = ast_.advect_fires(fire(DELHI_LAT, 73.6), uniform_wind(-8.0, 0.0))
    assert not bool(out.iloc[0]["hit"])


def test_parcel_stops_when_the_wind_record_ends():
    # Only 6 h of wind; the fire is ~13 h away. Unobservable, so no hit (and no crash).
    out = ast_.advect_fires(fire(DELHI_LAT, WEST_300KM, time="2026-10-01 00:00"), uniform_wind(5.0, 0.0, hours=6))
    assert not bool(out.iloc[0]["hit"])


def test_wind_interpolates_linearly_in_time():
    w = uniform_wind(0.0, 0.0, hours=6)
    w.u[1] = 6.0  # the 03:00 field is 6 m/s; 00:00 and 06:00 are 0
    u, _, ok = ast_.wind_at(w, np.array([np.datetime64("2026-10-01T01:30")]), np.array([28.0]), np.array([77.0]))
    assert ok[0] and u[0] == pytest.approx(3.0)


def test_wind_at_marks_a_missing_timestep_invalid_instead_of_bridging_it():
    w = uniform_wind(1.0, 0.0, hours=12)
    keep = [0, 1, 3, 4]  # drop the 06:00 slice -> a 6 h hole between 03:00 and 09:00
    w = ast_.WindField(w.times[keep], w.lats, w.lons, w.u[keep], w.v[keep])
    _, _, ok = ast_.wind_at(w, np.array([np.datetime64("2026-10-01T04:30")]), np.array([28.0]), np.array([77.0]))
    assert not ok[0]


def test_wind_at_rejects_points_outside_the_grid():
    w = uniform_wind(1.0, 0.0)
    t = np.array([np.datetime64("2026-10-01T03:00")] * 2)
    _, _, ok = ast_.wind_at(w, t, np.array([28.0, 40.0]), np.array([77.0, 77.0]))
    assert list(ok) == [True, False]


def test_daily_exposure_buckets_by_ist_arrival_day():
    traj = pd.DataFrame(
        {
            "hit": [True, True, True, False],
            "local": [False, False, False, False],
            # 19:00 UTC = 00:30 IST next day; 17:00 UTC = 22:30 IST same day
            "arrival_time": pd.to_datetime(["2026-10-01 19:00", "2026-10-01 17:00", "2026-10-01 17:30", None]),
            "frp": [10.0, 5.0, 1.0, 99.0],
        }
    )
    d = ast_.daily_exposure(traj).set_index("date")
    assert d.loc["2026-10-02", "hit_frp"] == 10.0
    assert d.loc["2026-10-01", "hit_frp"] == 6.0
    assert d.loc["2026-10-01", "hit_fires"] == 2


def test_valid_exposure_days_excludes_days_the_wind_cannot_cover():
    w = uniform_wind(1.0, 0.0, start="2026-09-23", hours=24 * 18)  # 09-23 00:00 .. 10-11 00:00 UTC
    first, last = ast_.valid_exposure_days(w)
    # 09-23 00:00 UTC + 72 h = 09-26 00:00 UTC = 05:30 IST -> the first FULL IST day after that is 09-27
    assert first == "2026-09-27"
    # last wind step 10-11 00:00 UTC = 10-11 05:30 IST: IST day 10-10 ends 10-11 00:00 IST <= that
    assert last == "2026-10-10"


def test_permutation_spearman_detects_real_and_ignores_fake_relationships():
    rng = np.random.default_rng(1)
    x = rng.normal(size=40)
    rho, p = ast_.permutation_spearman(x, x + rng.normal(scale=0.3, size=40), n_perm=2000)
    assert rho > 0.8 and p < 0.01
    _, p2 = ast_.permutation_spearman(x, rng.normal(size=40), n_perm=2000)
    assert p2 > 0.05


def test_partial_spearman_removes_a_shared_confounder():
    # x and y are both just driven by z: strongly correlated raw, ~0 once z is removed.
    rng = np.random.default_rng(2)
    z = rng.normal(size=60)
    x, y = z + rng.normal(scale=0.4, size=60), z + rng.normal(scale=0.4, size=60)
    raw, _ = ast_.permutation_spearman(x, y, n_perm=500)
    part, p = ast_.partial_spearman(x, y, z, n_perm=2000)
    assert raw > 0.6
    assert abs(part) < 0.3 and p > 0.05


def test_partial_spearman_keeps_a_relationship_that_z_does_not_explain():
    rng = np.random.default_rng(3)
    z = rng.normal(size=60)
    x = rng.normal(size=60)
    y = x + 0.5 * z + rng.normal(scale=0.3, size=60)
    part, p = ast_.partial_spearman(x, y, z, n_perm=2000)
    assert part > 0.7 and p < 0.01


def test_load_fires_drops_low_confidence_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(ast_, "RAW_DIR", tmp_path)
    base = {"source": "A", "longitude": 75.0, "acq_date": "2026-10-01", "acq_time": "0835", "frp": 5.0}
    rows = [
        {**base, "latitude": 30.0, "confidence": "n"},
        {**base, "latitude": 30.0, "confidence": "n"},  # duplicate
        {**base, "latitude": 31.0, "confidence": "l"},  # low confidence
        {**base, "latitude": 32.0, "confidence": "h"},
    ]
    pd.DataFrame(rows).to_csv(tmp_path / "firms_acq_2026-10-01.csv", index=False)
    out = ast_.load_fires()
    assert sorted(out["latitude"]) == [30.0, 32.0]
    assert out["fire_time"].iloc[0] == pd.Timestamp("2026-10-01 08:35")


def test_delhi_daily_requires_enough_hours_and_only_delhi_stations():
    rows = []
    for hour in range(24):
        ts = f"2026-10-05T{hour:02d}:00:00+05:30"
        rows.append({"hour_start_ist": ts, "station": "D1", "parameter": "pm25", "value": 100.0})
        rows.append({"hour_start_ist": ts, "station": "N1", "parameter": "pm25", "value": 900.0})  # Noida: excluded
        rows.append({"hour_start_ist": ts, "station": "D1", "parameter": "wind_speed", "value": 2.0})
    for hour in range(6):  # a second day with only 6 hours -> dropped
        rows.append({"hour_start_ist": f"2026-10-06T{hour:02d}:00:00+05:30", "station": "D1", "parameter": "pm25", "value": 50.0})
    out = ast_.delhi_daily_pm_and_wind(pd.DataFrame(rows), {"D1": "Delhi", "N1": "Noida"})
    assert list(out["date"]) == ["2026-10-05"]
    assert out.iloc[0]["pm25_delhi"] == 100.0 and out.iloc[0]["wind_delhi"] == 2.0


def test_circular_shift_p_cannot_go_below_one_over_n():
    # Even a perfect relationship: only n-1 shifts exist, so p >= ~1/n. At n=13 it cannot
    # reach 0.05 -- the test is honest about how little 13 days can prove.
    x = np.arange(13, dtype=float)
    rho, p = ast_.circular_shift_p(x, x.copy())
    assert rho == pytest.approx(1.0)
    assert p >= 1 / 13 - 1e-9
    assert p < 0.1


def test_circular_shift_p_is_not_fooled_by_a_shared_trend_unlike_iid_shuffling():
    # Two unrelated random walks both drift, so they correlate by accident. Independent
    # shuffling calls that significant far more often than circular shifting does.
    rng = np.random.default_rng(7)
    iid_hits = circ_hits = 0
    for _ in range(60):
        a, b = np.cumsum(rng.normal(size=25)), np.cumsum(rng.normal(size=25))
        iid_hits += ast_.permutation_spearman(a, b, n_perm=300, seed=1)[1] < 0.05
        circ_hits += ast_.circular_shift_p(a, b)[1] < 0.05
    assert iid_hits > circ_hits


def test_circular_shift_p_handles_degenerate_input():
    rho, p = ast_.circular_shift_p(np.ones(10), np.arange(10, dtype=float))
    assert np.isnan(rho) and np.isnan(p)
    assert np.isnan(ast_.circular_shift_p(np.arange(3.0), np.arange(3.0))[0])


def test_association_table_reports_exposed_days_only_correlation():
    rng = np.random.default_rng(4)
    n = 16
    x = np.r_[np.zeros(4), rng.uniform(1, 10, n - 4)]  # 4 clean days, then exposure
    pm = np.r_[np.full(4, 15.0), 40 + 5 * x[4:] + rng.normal(scale=2, size=n - 4)]
    daily = pd.DataFrame({"pm25_delhi": pm, "wind_delhi": rng.uniform(0.5, 1.5, n), "e": x})
    row = ast_.association_table(daily, ["e"]).query("lag_days == 0").iloc[0]
    assert row["n_exposed_days"] == n - 4
    assert row["spearman_exposed_days_only"] > 0.8  # amount matters beyond mere presence
    assert row["circ_shift_p"] >= 1 / n - 1e-3  # the table rounds p to 3 decimals

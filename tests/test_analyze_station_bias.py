import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_station_bias as asb  # noqa: E402


def make_network(n_stations=40, n_days=12, seed=0, signal=True, biased=None, ramp=None, decoupled=()):
    """Synthetic city: PM2.5 = day_level * AOD^0.8 * lognormal noise.
    `biased`: {station: multiplier} applied every day. `ramp`: {station: final multiplier},
    growing linearly from 1 over the period."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        date = (pd.Timestamp("2026-09-26") + pd.Timedelta(days=d)).strftime("%Y-%m-%d")
        day_level = rng.uniform(40, 120)  # whole-city effect (boundary layer etc.) -- must cancel out
        for s in range(n_stations):
            aod = float(np.exp(rng.normal(0, 0.5)))
            pm = day_level * aod**0.8 if signal else day_level * float(np.exp(rng.normal(0, 0.5)))
            pm *= float(np.exp(rng.normal(0, 0.12)))
            name = f"S{s}"
            if name in decoupled:  # this station's PM2.5 ignores the satellite pattern entirely
                pm = day_level * float(np.exp(rng.normal(0, 0.5)))
            if biased and name in biased:
                pm *= biased[name]
            if ramp and name in ramp:
                pm *= 1 + (ramp[name] - 1) * d / (n_days - 1)
            rows.append({"station": name, "date": date, "pm25": pm, "aod": aod})
    return pd.DataFrame(rows)


def run(joined):
    resid, day_stats = asb.daily_residuals(joined)
    ok, rho = asb.signal_gate(day_stats)
    return asb.station_scores(resid, ok), ok, rho


def test_flags_a_station_reading_persistently_high_and_nothing_else():
    scores, ok, _ = run(make_network(biased={"S7": 2.2}))
    assert ok
    flagged = scores[scores["flag"]]
    assert list(flagged["station"]) == ["S7"]
    assert "HIGH" in flagged.iloc[0]["reason"]


def test_flags_a_station_reading_persistently_low():
    scores, _, _ = run(make_network(biased={"S3": 0.4}))
    flagged = scores[scores["flag"]]
    assert list(flagged["station"]) == ["S3"]
    assert "LOW" in flagged.iloc[0]["reason"]


def test_citywide_day_to_day_swings_do_not_cause_flags():
    # day_level varies 40-120 between days; a clean network must produce no flags.
    scores, ok, _ = run(make_network(seed=3))
    assert ok
    assert not scores["flag"].any()


def test_no_satellite_signal_means_no_flags_even_with_a_biased_station():
    # Gate: when PM2.5 is unrelated to AOD, a "bias" is meaningless -- stay silent.
    scores, ok, rho = run(make_network(signal=False, biased={"S7": 3.0}))
    assert not ok
    assert rho < asb.MIN_SIGNAL_RHO
    assert not scores["flag"].any()
    assert scores["reason"].str.contains("signal too weak").all()


def test_too_few_days_is_not_flagged():
    scores, _, _ = run(make_network(n_days=4, biased={"S7": 3.0}))
    row = scores[scores["station"] == "S7"].iloc[0]
    assert not row["flag"]
    assert "insufficient days" in row["reason"]


def test_days_with_too_few_stations_are_skipped():
    resid, day_stats = asb.daily_residuals(make_network(n_stations=asb.MIN_STATIONS_PER_DAY - 1))
    assert resid.empty and day_stats.empty


def test_ramping_station_shows_a_positive_trend():
    scores, _, _ = run(make_network(n_days=14, ramp={"S5": 3.0}))
    s5 = scores[scores["station"] == "S5"].iloc[0]
    other = scores[scores["station"] != "S5"]["trend_z_per_day"].abs().max()
    assert s5["trend_z_per_day"] > 0.1
    assert s5["trend_z_per_day"] > 3 * other


def test_trend_is_withheld_without_enough_history():
    scores, _, _ = run(make_network(n_days=asb.MIN_DAYS_FOR_TREND - 2))
    assert scores["trend_z_per_day"].isna().all()


def test_ground_window_daily_averages_the_satellite_window_only():
    rows = []
    for hour, val in [(8, 999.0), (10, 10.0), (11, 20.0), (12, 30.0), (14, 40.0), (16, 999.0)]:
        rows.append({"hour_start_ist": f"2026-10-05T{hour:02d}:00:00+05:30", "station": "A", "parameter": "pm25", "value": val})
    rows.append({"hour_start_ist": "2026-10-05T11:00:00+05:30", "station": "A", "parameter": "pm10", "value": 5000.0})
    out = asb.ground_window_daily(pd.DataFrame(rows))
    assert len(out) == 1
    assert out.iloc[0]["pm25"] == 25.0  # mean of 10, 20, 30, 40; hours 8/16 and pm10 excluded
    assert out.iloc[0]["n_hours"] == 4


def test_ground_window_daily_drops_days_with_too_few_hours():
    rows = [{"hour_start_ist": f"2026-10-05T{h:02d}:00:00+05:30", "station": "A", "parameter": "pm25", "value": 50.0} for h in (10, 11)]
    assert asb.ground_window_daily(pd.DataFrame(rows)).empty


def test_join_satellite_drops_masked_and_nonpositive_rows():
    ground = pd.DataFrame({"station": ["A", "B", "C", "D"], "date": ["d"] * 4, "pm25": [50.0, 60.0, 70.0, 0.0], "n_hours": [5] * 4})
    aod = pd.DataFrame({"station": ["A", "B", "C", "D"], "date": ["d"] * 4, "aod_055": [0.5, np.nan, -0.1, 0.4]})
    out = asb.join_satellite(ground, aod)
    assert list(out["station"]) == ["A"]  # B masked (NaN), C negative AOD, D zero PM


def test_join_satellite_tolerates_duplicate_aod_rows():
    ground = pd.DataFrame({"station": ["A"], "date": ["d"], "pm25": [50.0], "n_hours": [5]})
    aod = pd.DataFrame({"station": ["A", "A"], "date": ["d", "d"], "aod_055": [0.5, 0.6]})
    assert len(asb.join_satellite(ground, aod)) == 1


# ---------------------------------------------------------------- temporal detector


def test_temporal_flags_a_station_decoupled_from_the_satellite():
    out = asb.temporal_decoupling(make_network(n_days=30, decoupled={"S9"}))
    flagged = out[out["flag"]]
    assert list(flagged["station"]) == ["S9"]
    assert flagged.iloc[0]["reason"] == "PM2.5 decoupled from satellite vs peers"


def test_temporal_clean_network_flags_nothing_and_reasons_are_correct():
    # Regression: `~signal_ok` on a Python bool (== -2, truthy) labelled every row
    # "network itself does not track the satellite" even when it clearly did.
    out = asb.temporal_decoupling(make_network(n_days=14, seed=5))
    assert not out["flag"].any()
    assert (out["reason"] == "tracks satellite like the network").all()
    assert out["network_median_rho"].iloc[0] > asb.MIN_SIGNAL_RHO


def test_temporal_multiple_comparison_control_prevents_chance_flags():
    # 50 clean stations, short history: the lowest few rho values look "decoupled"
    # by chance alone. FDR control must keep that from becoming a flag, across seeds.
    for seed in range(8):
        out = asb.temporal_decoupling(make_network(n_stations=50, n_days=9, seed=seed))
        assert not out["flag"].any(), f"false positive with seed {seed}"


def test_temporal_gate_off_when_network_does_not_track_satellite():
    out = asb.temporal_decoupling(make_network(n_days=20, signal=False, decoupled={"S1"}))
    assert not out["flag"].any()
    assert (out["reason"] == "no flag: network itself does not track the satellite").all()


def test_temporal_needs_enough_days_per_station():
    assert asb.temporal_decoupling(make_network(n_days=asb.MIN_DAYS_TO_FLAG - 2)).empty


def test_temporal_q_values_are_monotone_in_p_and_bounded():
    out = asb.temporal_decoupling(make_network(n_stations=30, n_days=10, seed=2)).sort_values("p_low")
    assert (out["q_value"].diff().dropna() >= -1e-9).all()
    assert out["q_value"].between(0, 1).all()
    assert (out["q_value"] >= out["p_low"] - 1e-9).all()


def test_temporal_borderline_station_is_labelled_watch_not_clean():
    # With little history a decoupled station can have a small raw p-value yet fail
    # FDR. It must not be labelled "tracks satellite like the network" -- it doesn't.
    out = asb.temporal_decoupling(make_network(n_stations=40, n_days=9, seed=1, decoupled={"S4"}))
    row = out[out["station"] == "S4"].iloc[0]
    assert row["rho"] < out["network_median_rho"].iloc[0]
    assert row["reason"] != "tracks satellite like the network"

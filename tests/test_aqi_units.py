import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import aqi_units as au  # noqa: E402
import convert_feed as cf  # noqa: E402
import validate_feed_units as vf  # noqa: E402


# ---------------------------------------------------------------- the tables


def test_sub_index_hits_the_published_knots_exactly():
    assert au.sub_index("PM2.5", 30) == 50 and au.sub_index("PM2.5", 60) == 100 and au.sub_index("PM2.5", 90) == 200
    assert au.sub_index("PM10", 100) == 100 and au.sub_index("PM10", 250) == 200
    assert au.sub_index("NO2", 80) == 100 and au.sub_index("SO2", 380) == 200
    assert au.sub_index("CO", 2) == 100 and au.sub_index("OZONE", 168) == 200


def test_sub_index_interpolates_linearly_between_knots_and_clamps_at_the_ends():
    assert au.sub_index("PM2.5", 45) == pytest.approx(75.0)  # halfway between (30,50) and (60,100)
    assert au.sub_index("PM2.5", 0) == 0 and au.sub_index("PM2.5", -5) == 0
    assert au.sub_index("PM2.5", 9999) == 500


@pytest.mark.parametrize("pollutant", list(au.BREAKPOINTS))
def test_tables_are_strictly_increasing_so_they_can_be_inverted(pollutant):
    xs, ys = zip(*au.BREAKPOINTS[pollutant])
    assert all(b > a for a, b in zip(xs, xs[1:])) and all(b > a for a, b in zip(ys, ys[1:]))


@pytest.mark.parametrize("pollutant", list(au.BREAKPOINTS))
def test_inverse_round_trips_everywhere_below_the_ceiling(pollutant):
    top = au.BREAKPOINTS[pollutant][-1][0]
    conc = np.linspace(0, top * 0.999, 200)
    back = au.concentration_from_subindex(pollutant, au.sub_index(pollutant, conc))
    assert np.allclose(back, conc, rtol=1e-9, atol=1e-9)


def test_inverse_is_monotone_and_censors_the_ceiling():
    idx = np.arange(0, 501, 5)
    conc = au.concentration_from_subindex("PM2.5", idx)
    assert np.all(np.diff(conc) > 0)
    assert list(au.is_censored([499.0, 500.0, 520.0])) == [False, True, True]
    assert au.concentration_from_subindex("PM2.5", 800) == au.concentration_from_subindex("PM2.5", 500)  # clamped: a lower bound


def test_every_feed_pollutant_has_a_statistic_and_unit():
    assert set(au.STATISTIC) == set(au.BREAKPOINTS) == set(au.UNITS)
    assert au.STATISTIC["PM2.5"] == "mean24" and au.STATISTIC["CO"] == "mean8" and au.UNITS["CO"] == "mg/m3"


# ---------------------------------------------------------------- trailing statistics


def test_trailing_stat_requires_enough_valid_hours():
    h = pd.DataFrame({"A": np.r_[np.full(10, np.nan), np.full(20, 10.0)]}, index=pd.date_range("2026-10-01", periods=30, freq="h"))
    m24 = vf.trailing_stat(h, "mean24")["A"]
    assert m24.iloc[:25].isna().all()  # fewer than 16 valid hours in the window
    assert m24.iloc[26] == pytest.approx(10.0)  # 16+ valid hours: defined
    assert vf.trailing_stat(h, "mean8")["A"].iloc[17] == pytest.approx(10.0)  # 8 h window with >= 6 valid


def test_max8_takes_the_peak_of_the_rolling_eight_hour_mean():
    vals = np.r_[np.full(20, 10.0), np.full(8, 50.0), np.full(8, 10.0)]
    h = pd.DataFrame({"A": vals}, index=pd.date_range("2026-10-01", periods=len(vals), freq="h"))
    assert vf.trailing_stat(h, "max8")["A"].iloc[-1] == pytest.approx(50.0)
    assert vf.trailing_stat(h, "mean8")["A"].iloc[-1] == pytest.approx(10.0)  # the CURRENT 8 h mean has come back down


# ---------------------------------------------------------------- the validation logic itself


def _openaq(station, parameter, value, hours=40):
    ts = pd.date_range("2026-10-01", periods=hours, freq="h")
    return pd.DataFrame({"hour_start_ist": [t.strftime("%Y-%m-%dT%H:%M:%S+05:30") for t in ts], "station": station, "parameter": parameter, "value": value})


def _feed(station, hour, **cols):
    return pd.DataFrame([{"station": station, "hour": pd.Timestamp(hour), **cols}])


def test_compare_recognises_a_feed_that_carries_sub_indices():
    oq = _openaq("A", "pm25", 45.0)
    cmp = vf.compare(_feed("A", "2026-10-02 12:00", **{"PM2.5": au.sub_index("PM2.5", 45.0)}), oq)
    row = cmp.iloc[0]
    assert row["feed"] == pytest.approx(75.0) and row["h1_subindex"] == pytest.approx(75.0)
    assert abs(row["feed"] - row["h2_mean24"]) > 20  # and clearly NOT the raw concentration


def test_compare_recognises_a_feed_that_carries_raw_concentrations():
    oq = _openaq("A", "pm25", 45.0)
    row = vf.compare(_feed("A", "2026-10-02 12:00", **{"PM2.5": 45.0}), oq).iloc[0]
    assert abs(row["feed"] - row["h1_subindex"]) > 20 and row["feed"] == pytest.approx(row["h2_mean24"])


def test_compare_skips_readings_openaq_cannot_reproduce():
    oq = _openaq("A", "pm25", 45.0, hours=10)  # too short for a 24 h mean
    assert vf.compare(_feed("A", "2026-10-01 09:00", **{"PM2.5": 75.0}), oq).empty
    assert vf.compare(_feed("Z", "2026-10-02 12:00", **{"PM2.5": 75.0}), _openaq("A", "pm25", 45.0)).empty  # unknown station


def test_inversion_error_is_tiny_when_the_feed_really_is_a_sub_index():
    oq = _openaq("A", "pm25", 45.0, hours=72)  # long enough that every feed hour below has a full 24 h behind it
    feed = pd.concat([_feed("A", f"2026-10-02 {h:02d}:00", **{"PM2.5": au.sub_index("PM2.5", 45.0)}) for h in (6, 12, 18)] * 4)
    inv = vf.inversion_error(vf.compare(feed.reset_index(drop=True), oq))
    assert len(inv) == 1 and inv.iloc[0]["median_rel_err_pct"] < 0.1


# ---------------------------------------------------------------- the converter


def _wide(**cols):
    base = {"station": " ITO, Delhi - CPCB ", "last_update": "05-10-2026 12:00:00", "PM2.5": 75.0, "PM10": 121.0, "NO2": 50.0,
            "SO2": 10.0, "OZONE": 40.0, "CO": 50.0}
    base.update(cols)
    return pd.DataFrame([base])


def test_convert_inverts_every_pollutant_and_labels_units_and_statistic():
    out = cf.convert(_wide()).set_index("pollutant")
    assert out.loc["PM2.5", "concentration"] == pytest.approx(45.0)  # sub-index 75 <- 45 ug/m3
    assert out.loc["CO", "concentration"] == pytest.approx(1.0) and out.loc["CO", "unit"] == "mg/m3"
    assert out.loc["PM2.5", "statistic"] == "mean24" and out.loc["OZONE", "statistic"] == "mean8"
    assert set(out.index) == {"PM2.5", "PM10", "NO2", "SO2", "OZONE", "CO"}


def test_convert_strips_station_names_and_floors_the_timestamp_to_the_hour():
    out = cf.convert(_wide(last_update="05-10-2026 12:00:00"))
    assert (out["station"] == "ITO, Delhi - CPCB").all()
    assert (out["hour_ist"] == pd.Timestamp("2026-10-05 12:00")).all()


def test_convert_marks_ceiling_readings_as_censored():
    out = cf.convert(_wide(**{"PM2.5": 500.0})).set_index("pollutant")
    assert bool(out.loc["PM2.5", "censored"]) and not bool(out.loc["PM10", "censored"])


def test_convert_drops_missing_negative_and_unparseable_values():
    out = cf.convert(_wide(**{"PM2.5": np.nan, "PM10": -4.0, "NO2": "NA"}))
    assert set(out["pollutant"]) == {"SO2", "OZONE", "CO"}


def test_convert_deduplicates_repeated_readings_keeping_the_last():
    twice = pd.concat([_wide(**{"PM2.5": 50.0}), _wide(**{"PM2.5": 75.0})], ignore_index=True)
    pm = cf.convert(twice).query("pollutant == 'PM2.5'")
    assert len(pm) == 1 and pm.iloc[0]["subindex"] == 75.0


def test_convert_handles_an_empty_or_unrecognised_frame():
    assert cf.convert(pd.DataFrame({"station": [], "last_update": []})).empty
    assert cf.convert(_wide().drop(columns=["PM2.5", "PM10", "NO2", "SO2", "OZONE", "CO"])).empty

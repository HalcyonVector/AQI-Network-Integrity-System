import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_openaq  # noqa: E402


@pytest.fixture
def logger():
    log = logging.getLogger("test_fetch_openaq")
    log.addHandler(logging.NullHandler())
    return log


def test_archive_url_matches_real_layout():
    url = fetch_openaq.archive_url(5626, datetime(2026, 10, 5))
    assert url.endswith("/locationid=5626/year=2026/month=10/location-5626-20261005.csv.gz")


def test_circular_mean_wraps_around_north():
    # Arithmetic mean of 350 and 10 is 180 (south); the real mean direction is north.
    out = fetch_openaq.circular_mean_deg(pd.Series([350.0, 10.0]))
    assert out == pytest.approx(0.0, abs=1e-6) or out == pytest.approx(360.0, abs=1e-6)


def test_circular_mean_empty_is_nan():
    assert pd.isna(fetch_openaq.circular_mean_deg(pd.Series([], dtype=float)))


def _records(rows):
    return pd.DataFrame(rows, columns=["location_id", "datetime", "parameter", "units", "value"])


def test_hourly_long_averages_15min_samples():
    recs = _records(
        [
            (5626, "2026-10-05T03:00:00+05:30", "pm25", "µg/m³", 10.0),
            (5626, "2026-10-05T03:15:00+05:30", "pm25", "µg/m³", 20.0),
            (5626, "2026-10-05T03:30:00+05:30", "pm25", "µg/m³", 30.0),
            (5626, "2026-10-05T03:45:00+05:30", "pm25", "µg/m³", 40.0),
        ]
    )
    out = fetch_openaq.to_hourly_long(recs, "DTU", datetime(2026, 10, 5))
    assert len(out) == 1
    assert out.iloc[0]["value"] == 25.0
    assert out.iloc[0]["n_obs"] == 4
    assert out.iloc[0]["station"] == "DTU"
    assert out.iloc[0]["hour_start_ist"].startswith("2026-10-05T03:00:00")


def test_hourly_long_keeps_units_per_parameter():
    # Real archive data mixes units within one station (NO2 in ppb, O3 in ug/m3).
    recs = _records(
        [
            (5626, "2026-10-05T03:00:00+05:30", "no2", "ppb", 15.0),
            (5626, "2026-10-05T03:00:00+05:30", "o3", "µg/m³", 30.0),
        ]
    )
    out = fetch_openaq.to_hourly_long(recs, "DTU", datetime(2026, 10, 5)).set_index("parameter")
    assert out.loc["no2", "units"] == "ppb"
    assert out.loc["o3", "units"] == "µg/m³"


def test_hourly_long_drops_rows_outside_requested_ist_day():
    recs = _records(
        [
            (5626, "2026-10-04T23:45:00+05:30", "pm25", "µg/m³", 99.0),  # previous day
            (5626, "2026-10-05T00:00:00+05:30", "pm25", "µg/m³", 10.0),
            (5626, "2026-10-06T00:00:00+05:30", "pm25", "µg/m³", 77.0),  # next day
        ]
    )
    out = fetch_openaq.to_hourly_long(recs, "DTU", datetime(2026, 10, 5))
    assert list(out["value"]) == [10.0]


def test_hourly_long_ignores_unwanted_parameters_and_empty_input():
    recs = _records([(5626, "2026-10-05T03:00:00+05:30", "bc", "µg/m³", 5.0)])
    assert fetch_openaq.to_hourly_long(recs, "DTU", datetime(2026, 10, 5)).empty
    assert fetch_openaq.to_hourly_long(None, "DTU", datetime(2026, 10, 5)).empty
    assert fetch_openaq.to_hourly_long(recs.iloc[0:0], "DTU", datetime(2026, 10, 5)).empty


def test_hourly_long_wind_direction_uses_circular_mean():
    recs = _records(
        [
            (5626, "2026-10-05T03:00:00+05:30", "wind_direction", "deg", 350.0),
            (5626, "2026-10-05T03:15:00+05:30", "wind_direction", "deg", 10.0),
        ]
    )
    out = fetch_openaq.to_hourly_long(recs, "DTU", datetime(2026, 10, 5))
    v = out.iloc[0]["value"]
    assert min(v, 360 - v) < 1.0  # ~0/360, not 180


def _ours(rows):
    return pd.DataFrame(rows, columns=["station", "latitude", "longitude"])


def _oq(rows):
    return pd.DataFrame(rows, columns=["id", "name", "lat", "lon"])


def test_match_by_coordinates():
    ours = _ours([("DTU, Delhi - CPCB", 28.7500, 77.1112)])
    oq = _oq([(5626, "DTU, New Delhi - CPCB-3379478", 28.7500, 77.1112)])
    m = fetch_openaq.match_stations(ours, oq)
    assert m.iloc[0]["openaq_location_id"] == 5626
    assert m.iloc[0]["match_method"] == "coordinates"


def test_match_falls_back_to_identical_name_when_coordinates_differ():
    # Real case: Sector-1 Noida has the same name in both sources but coordinates
    # that differ by ~1.2 km.
    ours = _ours([("Sector-1, Noida - UPPCB", 28.5900, 77.3200)])
    oq = _oq([(6980, "Sector-1, Noida - UPPCB", 28.6000, 77.3300)])
    m = fetch_openaq.match_stations(ours, oq)
    assert m.iloc[0]["openaq_location_id"] == 6980
    assert m.iloc[0]["match_method"] == "name"


def test_match_refuses_a_nearby_but_different_station():
    # Real case: the nearest active OpenAQ location to some IITM-run stations is a
    # different station kilometres away. Better unmapped than wrongly mapped.
    ours = _ours([("North Campus, DU, Delhi - IITM", 28.6900, 77.2100)])
    oq = _oq([(8917, "Ashok Vihar, Delhi - DPCC", 28.7000, 77.1800)])
    m = fetch_openaq.match_stations(ours, oq)
    assert m.empty


def test_match_prefers_nearest_of_several_candidates():
    ours = _ours([("Anand Vihar", 28.6470, 77.3150)])
    oq = _oq([(1, "Far one", 28.6475, 77.3150), (2, "Near one", 28.6470, 77.3151)])
    assert fetch_openaq.match_stations(ours, oq).iloc[0]["openaq_location_id"] == 2


def test_match_handles_missing_coordinates_via_name():
    ours = _ours([("Foo, Delhi - DPCC", None, None)])
    oq = _oq([(9, "Foo, Delhi - DPCC", 28.6, 77.2)])
    assert fetch_openaq.match_stations(ours, oq).iloc[0]["openaq_location_id"] == 9


def test_select_active_locations_keeps_cpcb_and_na_drops_lowcost_and_stale():
    now = pd.Timestamp("2026-10-12", tz="UTC")
    locs = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "name": ["cpcb", "na-provider", "low-cost", "stale", "never"],
            "provider": ["CPCB", "N/A", "AirGradient", "CPCB", "CPCB"],
            "last": ["2026-10-07T14:30:00Z", "2026-10-07T14:30:00Z", "2026-10-07T14:30:00Z", "2018-02-22T04:00:00Z", None],
        }
    )
    out = fetch_openaq.select_active_locations(locs, now)
    # Regression: provider "N/A" holds real DPCC/UPPCB stations (JNU, Wave City, ...).
    assert list(out["name"]) == ["cpcb", "na-provider"]


def test_warn_on_unexpected_pm_units(logger, caplog):
    df = pd.DataFrame({"parameter": ["pm25"], "units": ["ppb"]})
    with caplog.at_level(logging.WARNING, logger="test_fetch_openaq"):
        fetch_openaq.warn_on_unexpected_units(df, logger)
    assert "non-ug/m3" in caplog.text

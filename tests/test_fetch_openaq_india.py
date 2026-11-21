import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_openaq_india as fi  # noqa: E402


def _rec():
    # 2026-11-20 18:30 UTC = 2026-11-21 00:00 IST
    t = pd.to_datetime(["2026-11-20T18:30:00Z", "2026-11-20T18:45:00Z", "2026-11-20T19:30:00Z", "2026-11-20T18:30:00Z", "2026-11-20T18:30:00Z", "2026-11-20T18:00:00Z"])
    return pd.DataFrame({"datetime": t.astype(str), "parameter": ["pm25", "pm25", "pm25", "pm10", "pm25", "pm25"],
                         "units": ["µg/m³"] * 4 + ["ppb", "µg/m³"], "value": [100.0, 120.0, 50.0, 200.0, 9.0, 7.0]})


def test_hourly_pm_means_by_ist_hour_keeps_only_ugm3_and_the_requested_day():
    out = fi.hourly_pm(_rec(), 42, datetime(2026, 11, 21))
    h0 = out[out["hour"] == 0].iloc[0]
    assert h0["pm25"] == 110.0 and h0["pm10"] == 200.0 and h0["openaq_id"] == 42
    assert out[out["hour"] == 1].iloc[0]["pm25"] == 50.0 and pd.isna(out[out["hour"] == 1].iloc[0]["pm10"])
    assert len(out) == 2  # the 23:30 IST reading belongs to the previous day; the ppb row is dropped


def test_hourly_pm_empty_inputs():
    assert fi.hourly_pm(None, 1, datetime(2026, 11, 21)).empty
    assert fi.hourly_pm(pd.DataFrame(), 1, datetime(2026, 11, 21)).empty

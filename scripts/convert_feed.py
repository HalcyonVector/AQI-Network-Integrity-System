"""
Turn the data.gov.in feed's sub-index values back into concentrations.

The feed's pollutant columns are CPCB AQI sub-indices (see aqi_units.py, validated by
validate_feed_units.py). Because the tables are monotone they invert, which makes the feed usable as a
concentration source again -- including for stations that exist ONLY in the feed (the 13 Delhi DPCC
stations added in October 2026) and for dates OpenAQ does not cover yet.

What the converted numbers ARE: daily-scale averages, not hourly readings. PM2.5, PM10, NO2, SO2 are
trailing 24-hour means; ozone and CO are the current 8-hour mean. Never treat them as hourly.

Accuracy against OpenAQ over the pre-outage overlap (median relative error): PM2.5 2.9%, PM10 2.2%,
NO2 1.4%, SO2 2.4%, CO 3.2%. OZONE IS WORSE (median 9.6%, 90th percentile 30%): treat it with care.
Readings whose sub-index sits at the table's ceiling (500) are censored: the concentration is only a
lower bound (column `censored`).

Usage:
    python scripts/convert_feed.py
Writes data/processed/feed_concentrations.csv.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from aqi_units import STATISTIC, UNITS, concentration_from_subindex, is_censored

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed"


def convert(feed: pd.DataFrame) -> pd.DataFrame:
    """Wide feed frame (station, last_update, PM2.5, PM10, ...) -> long concentrations.

    Rows with a missing, negative or non-numeric sub-index are dropped. Duplicate (station, hour,
    pollutant) readings keep the last one.
    """
    feed = feed.copy()
    feed["station"] = feed["station"].astype(str).str.strip()
    feed["hour_ist"] = pd.to_datetime(feed["last_update"], format="%d-%m-%Y %H:%M:%S", errors="coerce").dt.floor("h")
    frames = []
    for pollutant in STATISTIC:
        if pollutant not in feed.columns:
            continue
        idx = pd.to_numeric(feed[pollutant], errors="coerce")
        keep = idx.notna() & (idx >= 0) & feed["hour_ist"].notna()
        g = feed.loc[keep, ["station", "hour_ist"]].copy()
        g["pollutant"] = pollutant
        g["subindex"] = idx[keep].to_numpy()
        g["concentration"] = np.round(concentration_from_subindex(pollutant, g["subindex"].to_numpy()), 3)
        g["unit"] = UNITS[pollutant]
        g["statistic"] = STATISTIC[pollutant]
        g["censored"] = is_censored(g["subindex"].to_numpy())
        frames.append(g)
    if not frames:
        return pd.DataFrame(columns=["station", "hour_ist", "pollutant", "subindex", "concentration", "unit", "statistic", "censored"])
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["station", "hour_ist", "pollutant"], keep="last")
    return out.sort_values(["station", "hour_ist", "pollutant"]).reset_index(drop=True)


def main() -> int:
    files = sorted(glob.glob(str(RAW_DIR / "cpcb_ncr_*.csv")))
    if not files:
        print("No data/raw/cpcb_ncr_*.csv files.")
        return 1
    feed = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    out = convert(feed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_DIR / "feed_concentrations.csv", index=False)
    print(f"{len(out)} converted readings | {out['station'].nunique()} stations | "
          f"{out['hour_ist'].min():%Y-%m-%d} .. {out['hour_ist'].max():%Y-%m-%d} | censored: {int(out['censored'].sum())}")
    print(out.groupby("pollutant")["concentration"].describe()[["count", "50%", "max"]].round(1).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())

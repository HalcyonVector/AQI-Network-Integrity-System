"""Silver layer: clean hourly PM2.5 and PM10 per station from the 15-minute CPCB records.

Per year file (data/historical/bronze/cpcb/) this
  1. converts the UTC timestamps to IST and averages the 15-minute readings into hours, keeping an hour
     only if at least MIN_READINGS of its 4 readings exist (CPCB's own 75% completeness rule);
  2. drops non-positive and implausibly large readings (scripts/qc.PM_MAX), the same range check as live;
  3. drops stuck-sensor runs (scripts/qc.mask_flatlines), the same flatline rule as live.
It writes data/historical/silver/pm_hourly_<year>.parquet (never committed) and a small summary of how
many station-hours each step removed to data/processed/historical/silver_qc_summary.csv (tracked).

CPCB reports PM in ug/m3 already, so no unit conversion is needed here (the live feed's sub-index
conversion in scripts/aqi_units.py only applies to the data.gov.in feed).

    python historical/build_silver.py
    python historical/build_silver.py --years 2019 2020
"""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc  # noqa: E402  (the live pipeline's range check and flatline rule)

BRONZE = ROOT / "data" / "historical" / "bronze" / "cpcb"
SILVER = ROOT / "data" / "historical" / "silver"
SUMMARY = ROOT / "data" / "processed" / "historical" / "silver_qc_summary.csv"
COLS = {"PM2.5 (µg/m³)": "pm25", "PM10 (µg/m³)": "pm10"}
MIN_READINGS = 3  # of the 4 quarter-hour readings in an hour
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)


def hourly_means(raw: pd.DataFrame) -> pd.DataFrame:
    """Hourly mean per station and parameter (IST hour start); hours with too few readings become NaN."""
    df = raw.rename(columns={"Station ID": "station_id", **COLS}).copy()
    ts = pd.to_datetime(df["Timestamp"], utc=True).dt.tz_localize(None)
    df["hour_start_ist"] = (ts + IST_OFFSET).dt.floor("h")
    g = df.groupby(["station_id", "hour_start_ist"])
    out = g[list(COLS.values())].mean()
    counts = g[list(COLS.values())].count()
    for p in COLS.values():
        out[p] = out[p].where(counts[p] >= MIN_READINGS)
    return out.reset_index()


def clean_hourly(hourly: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Apply the range and flatline checks; returns (clean long frame, counts of hours removed per step)."""
    stats = {"hours_raw": 0, "removed_range": 0, "removed_flatline": 0}
    cleaned = {}
    for p in COLS.values():
        m = hourly.pivot(index="hour_start_ist", columns="station_id", values=p)
        m = m.reindex(pd.date_range(m.index.min(), m.index.max(), freq="h"))  # gaps break flatline runs
        raw_n = int(m.notna().sum().sum())
        in_range = m.where((m > 0) & (m <= qc.PM_MAX))
        flat = qc.mask_flatlines(in_range)
        stats["hours_raw"] += raw_n if p == "pm25" else 0
        if p == "pm25":
            stats["removed_range"] = raw_n - int(in_range.notna().sum().sum())
            stats["removed_flatline"] = int(in_range.notna().sum().sum()) - int(flat.notna().sum().sum())
        long = flat.stack().rename(p).reset_index()
        long.columns = ["hour_start_ist", "station_id", p]
        cleaned[p] = long
    out = cleaned["pm25"].merge(cleaned["pm10"], on=["hour_start_ist", "station_id"], how="outer")
    stats["hours_clean"] = int(out["pm25"].notna().sum())
    stats["stations"] = int(out.loc[out["pm25"].notna(), "station_id"].nunique())
    return out[["station_id", "hour_start_ist", "pm25", "pm10"]].sort_values(["station_id", "hour_start_ist"]), stats


def build_year(year: int, bronze: Path = BRONZE, silver: Path = SILVER) -> dict:
    raw = pd.read_parquet(bronze / f"cpcb-air-quality-{year}.parquet", columns=["Station ID", "Timestamp", *COLS])
    clean, stats = clean_hourly(hourly_means(raw))
    silver.mkdir(parents=True, exist_ok=True)
    clean.to_parquet(silver / f"pm_hourly_{year}.parquet", index=False)
    return {"year": year, **stats}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", type=int, nargs="+")
    args = ap.parse_args()
    years = args.years or sorted(int(re.search(r"(\d{4})\.parquet$", f.name).group(1)) for f in BRONZE.glob("cpcb-air-quality-*.parquet"))
    rows = []
    for y in years:
        row = build_year(y)
        rows.append(row)
        print(f"{y}: {row['hours_clean']:,} clean PM2.5 station-hours at {row['stations']} stations "
              f"(removed {row['removed_range']:,} out of range, {row['removed_flatline']:,} flatlined)", flush=True)
    new = pd.DataFrame(rows)
    if SUMMARY.exists():  # keep other years from earlier runs
        old = pd.read_csv(SUMMARY)
        new = pd.concat([old[~old["year"].isin(new["year"])], new]).sort_values("year")
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(SUMMARY, index=False)
    print(f"summary -> {SUMMARY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

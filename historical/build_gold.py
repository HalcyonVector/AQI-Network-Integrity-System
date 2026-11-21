"""Gold exports that need no weather data: daily fire exposure, Delhi PM2.5, and national completeness.

Reads the FIRMS archive files (data/historical/firms/) and the silver hourly PM table
(data/historical/silver/), and writes three small tables to data/processed/historical/ (tracked):

  fire_daily.csv             date, source, detections, frp_sum      regional fire counts per day and satellite
  delhi_pm25_daily.csv       date, pm25, stations                   Delhi daily mean PM2.5 (Sep-Dec of each year)
  completeness_state_month.csv  state, month, stations, completeness   how much of each month's hours were reported

The fire table is regional totals only. The pre-registered stubble test counts fires along the 925 hPa wind
path to Delhi, which needs the ERA5 winds, so that join is a later step and nothing here is a test result.

    python historical/build_gold.py
"""
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FIRMS = ROOT / "data" / "historical" / "firms"
SILVER = ROOT / "data" / "historical" / "silver"
OUT = ROOT / "data" / "processed" / "historical"
MIN_HOURS_PER_DAY = 18  # CPCB's 75% rule for a daily mean


def fire_daily(firms_dir: Path = FIRMS) -> pd.DataFrame:
    parts = []
    for f in sorted(firms_dir.glob("firms_*.csv")):
        d = pd.read_csv(f, usecols=["source", "acq_date", "frp"])
        parts.append(d.groupby(["source", "acq_date"]).agg(detections=("frp", "size"), frp_sum=("frp", "sum")).reset_index())
    out = pd.concat(parts, ignore_index=True).rename(columns={"acq_date": "date"})
    out["frp_sum"] = out["frp_sum"].round(1)
    return out[["date", "source", "detections", "frp_sum"]].sort_values(["source", "date"])


def delhi_daily(silver_year: pd.DataFrame, delhi_ids: set[str]) -> pd.DataFrame:
    """Daily mean over Delhi stations of each station's daily mean (a station-day needs MIN_HOURS_PER_DAY hours)."""
    d = silver_year[silver_year["station_id"].isin(delhi_ids)].dropna(subset=["pm25"]).copy()
    d["date"] = d["hour_start_ist"].dt.normalize()
    per_station = d.groupby(["station_id", "date"])["pm25"].agg(["mean", "count"]).reset_index()
    per_station = per_station[per_station["count"] >= MIN_HOURS_PER_DAY]
    out = per_station.groupby("date").agg(pm25=("mean", "mean"), stations=("station_id", "nunique")).reset_index()
    out["pm25"] = out["pm25"].round(1)
    return out[(out["date"].dt.month >= 9)]


def completeness(silver_year: pd.DataFrame, state_of: dict[str, str]) -> pd.DataFrame:
    """Per state and month: stations that reported anything, and the mean share of the month's hours they reported."""
    d = silver_year.dropna(subset=["pm25"]).copy()
    d["month"] = d["hour_start_ist"].dt.to_period("M")
    d["state"] = d["station_id"].map(state_of)
    n = d.groupby(["state", "month", "station_id"]).size().rename("hours").reset_index()
    n["completeness"] = n["hours"] / n["month"].dt.days_in_month.div(1).mul(24)
    out = n.groupby(["state", "month"]).agg(stations=("station_id", "nunique"), completeness=("completeness", "mean")).reset_index()
    out["month"] = out["month"].astype(str)
    out["completeness"] = out["completeness"].round(3)
    return out


def main() -> int:
    master = pd.read_csv(OUT / "station_master.csv")
    mm = master.dropna(subset=["cpcb_id"])
    delhi_ids = set(mm.loc[mm["city"] == "Delhi", "cpcb_id"])
    state_of = dict(zip(mm["cpcb_id"], mm["state"]))
    OUT.mkdir(parents=True, exist_ok=True)
    fire_daily().to_csv(OUT / "fire_daily.csv", index=False)
    delhi, comp = [], []
    for f in sorted(SILVER.glob("pm_hourly_*.parquet")):
        year = int(re.search(r"(\d{4})\.parquet$", f.name).group(1))
        s = pd.read_parquet(f, columns=["station_id", "hour_start_ist", "pm25"])
        delhi.append(delhi_daily(s, delhi_ids))
        comp.append(completeness(s, state_of))
        print(f"{year} done", flush=True)
    pd.concat(delhi).to_csv(OUT / "delhi_pm25_daily.csv", index=False)
    pd.concat(comp).sort_values(["state", "month"]).to_csv(OUT / "completeness_state_month.csv", index=False)
    print("wrote fire_daily.csv, delhi_pm25_daily.csv, completeness_state_month.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

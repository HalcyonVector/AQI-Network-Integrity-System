"""
National hourly PM2.5 and PM10 from OpenAQ's open archive, for every CPCB station in the
historical station master that has an OpenAQ location id (about 500 stations across India).

This is the live national feed: Vonter's yearly files stop at 2025 and the data.gov.in feed
is only a current snapshot. The NCR pipeline (fetch_openaq.py) is untouched.

Usage:
    python scripts/fetch_openaq_india.py                       # last 10 days, idempotent
    python scripts/fetch_openaq_india.py --start 2026-09-23 --end 2026-11-21   # backfill

Output: data/raw/openaq_india_pm_YYYY-MM-DD.parquet, one file per IST observation date,
overwritten on re-run (the archive back-fills late, so recent days are re-read every run).
Columns: hour (0 to 23, IST), openaq_id, pm25, pm10 (ug/m3, one decimal). Only PM rows with
ug/m3 units are kept. Quality control (range, flatline) is applied downstream, not here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_openaq as fo  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
MASTER_PATH = PROJECT_ROOT / "data" / "processed" / "historical" / "station_master.csv"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_LOOKBACK_DAYS = 10
WORKERS = 8
PM = ("pm25", "pm10")


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / f"fetch_openaq_india_{datetime.now():%Y-%m-%d}.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("fetch_openaq_india")


def location_ids(master_path: Path = MASTER_PATH) -> list[int]:
    m = pd.read_csv(master_path, usecols=["openaq_id"])
    return sorted({int(v) for v in m["openaq_id"].dropna()})


def hourly_pm(records: pd.DataFrame | None, location_id: int, day: datetime) -> pd.DataFrame:
    """Archive records for one location and IST day -> one row per hour with pm25 and pm10 (ug/m3)."""
    cols = ["hour", "openaq_id", "pm25", "pm10"]
    if records is None or records.empty:
        return pd.DataFrame(columns=cols)
    df = records[records["parameter"].isin(PM) & records["units"].astype(str).str.contains("g/m", na=False)].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["ts"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(fo.IST)
    start = pd.Timestamp(day.strftime("%Y-%m-%d"), tz=fo.IST)
    df = df[(df["ts"] >= start) & (df["ts"] < start + pd.Timedelta(days=1))]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["hour"] = df["ts"].dt.hour
    wide = df.groupby(["hour", "parameter"])["value"].mean().unstack("parameter").reset_index()
    for p in PM:
        if p not in wide.columns:
            wide[p] = float("nan")
    wide["openaq_id"] = location_id
    wide[list(PM)] = wide[list(PM)].round(1)
    return wide[cols]


def fetch_date(ids: list[int], day: datetime, logger: logging.Logger) -> pd.DataFrame:
    def one(lid: int) -> pd.DataFrame:
        try:
            return hourly_pm(fo.fetch_archive_day(lid, day), lid, day)
        except requests.RequestException as exc:
            logger.warning("Fetch failed for location %s on %s: %s", lid, day.strftime("%Y-%m-%d"), exc)
            return pd.DataFrame()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        frames = [f for f in pool.map(one, ids) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["hour", "openaq_id", "pm25", "pm10"])
    out = pd.concat(frames, ignore_index=True)
    out["hour"] = out["hour"].astype("int8")
    out["openaq_id"] = out["openaq_id"].astype("int32")
    return out.sort_values(["openaq_id", "hour"], ignore_index=True)


def write_date_file(df: pd.DataFrame, day: datetime) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DATA_DIR / f"openaq_india_pm_{day:%Y-%m-%d}.parquet"
    tmp = path.with_suffix(".part")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    return path


def needs_fetch(day: datetime, today: datetime, now_ts: float, explicit: bool) -> bool:
    """A day is read when it has no file, when it is one of the last 3 days (the archive publishes
    late), or when its file is over 3 days old (to pick up days the archive back-filled later).
    An explicit --start/--end always reads everything, for backfills."""
    path = RAW_DATA_DIR / f"openaq_india_pm_{day:%Y-%m-%d}.parquet"
    if explicit or not path.exists() or (today - day).days <= 3:
        return True
    return now_ts - path.stat().st_mtime > 3 * 86400


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="YYYY-MM-DD (default: today-10 UTC)")
    parser.add_argument("--end", help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    args = parser.parse_args()
    logger = setup_logging()

    if not MASTER_PATH.exists():
        logger.error("No station master at %s; run historical/build_station_table.py first.", MASTER_PATH)
        return 1
    ids = location_ids()
    today = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else today - timedelta(days=1)
    start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else today - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    explicit = bool(args.start or args.end)
    now_ts = datetime.now().timestamp()
    day = start
    while day <= end:
        if not needs_fetch(day, today, now_ts, explicit):
            day += timedelta(days=1)
            continue
        df = fetch_date(ids, day, logger)
        if df.empty:
            logger.warning("No OpenAQ India data for %s (archive may not have published it yet).", day.strftime("%Y-%m-%d"))
        else:
            path = write_date_file(df, day)
            logger.info("%s: %d stations, %d rows, %.0f KB", path.name, df["openaq_id"].nunique(), len(df), path.stat().st_size / 1024)
        day += timedelta(days=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
